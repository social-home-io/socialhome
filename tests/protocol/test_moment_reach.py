"""Release-blocker protocol tests: a moment goes as far as its reach (v_96).

Marked ``@pytest.mark.security``.

A person chooses how far their moments go (1–5 hops, ``docs/protocol/
momentum.md`` § Reach). The reach rides under the origin signature —
reach 1 is the v_38 ``no_relay`` mark, 2 / 4 / 5 a signed ``reach`` field
(``moment-origin:v3:``), 3 nothing new — so a relay can neither strip,
raise nor add it. Against the real application registry and SQLite:

* the origin signs the effective reach (person, household maximum,
  protection) on the create and on its delete, and gates who gets it
  (reach 1 → ≥ v_38, reach 2 → ≥ v_96, 4–5 → everyone);
* a receiver refuses an invalid reach and a copy beyond its reach, and
  stores the reach it took;
* a relay forwards only while ``hop < min(reach, MOMENT_MAX_HOPS)``, counts
  a copy not sent by its origin as at least hop 2 (the relay clamp), sends
  a reach-carrying copy only to v_96 households, and relays a closer copy
  at most once more;
* a relay that strips, raises or adds ``reach`` breaks the origin
  signature, so the moment is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    capabilities_outbound_key,
    child_protection_service_key,
    db_key,
    federation_repo_key,
    federation_service_key,
    instance_config_repo_key,
    moment_service_key,
    peer_unpair_service_key,
    user_service_key,
)
from socialhome.config import Config
from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.moment import MOMENT_MAX_HOPS
from socialhome.federation.federation_service import FederationService
from socialhome.moment_origin import NO_RELAY_FIELD, REACH_FIELD, sign_moment_origin
from socialhome.services.resync_on_upgrade import request_capability_resync

pytestmark = pytest.mark.security

FET = FederationEventType
ORIGIN_KEY = generate_identity_keypair()
ORIGIN = "peer-origin"
RELAY = "peer-relay"
#: Paired households by the version they advertise (ORIGIN and RELAY at OURS).
OLD38 = "peer-v38"
OLD95 = "peer-v95"
NEW = "peer-v96"
EXPIRES = (datetime.now(UTC) + timedelta(days=5)).isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "reach.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, pk: bytes, version: int) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            pk.hex(),
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            version,
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key, OURS)
    await _seed_peer(db, RELAY, generate_identity_keypair().public_key, OURS)
    await _seed_peer(db, OLD38, generate_identity_keypair().public_key, 38)
    await _seed_peer(db, OLD95, generate_identity_keypair().public_key, 95)
    await _seed_peer(db, NEW, generate_identity_keypair().public_key, 96)
    # NEW said its people look 5 hops (decision C); the others said nothing.
    app[federation_service_key].set_peer_moment_look_hops(NEW, 5)
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-mia', ?, 'mia', 'Mia')",
        (ORIGIN,),
    )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    return app, db, sent


async def _deliver(app, event_type, payload, *, from_instance) -> None:
    for handler in app[federation_service_key]._event_registry.handlers_for(event_type):
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )


def _signed(event_type, moment_id: str, *, hop: int, reach: int | None = None) -> dict:
    """A create (or delete) by Mia on ORIGIN, signed with *reach*."""
    payload: dict = {
        "moment_id": moment_id,
        "author_user_id": "u-mia",
        "origin_instance_id": ORIGIN,
        "occurred_at": datetime.now(UTC).isoformat(),
        "hop_count": 1,
    }
    if event_type is FET.MOMENT_CREATED:
        payload.update(
            content="how far?",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            expires_at=EXPIRES,
        )
    if reach == 1:
        payload[NO_RELAY_FIELD] = True
    elif reach is not None and reach != 3:
        payload[REACH_FIELD] = reach
    signed = sign_moment_origin(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        event_type=event_type,
        payload=payload,
    )
    return {**signed, "hop_count": hop}


async def _row(db, moment_id: str):
    return await db.fetchone(
        "SELECT hop_count, reach, deleted_at FROM moments WHERE id=?", (moment_id,)
    )


def _relayed(sent, moment_id: str) -> list[tuple[str, int]]:
    return sorted(
        (to, payload["hop_count"])
        for to, _et, payload in sent
        if payload.get("moment_id") == moment_id
    )


# ═══ The gate constant ═══════════════════════════════════════════════════


def test_reach_is_gated_on_v96():
    assert FederationCapability.MIN_FOR_MOMENT_REACH == 96
    assert OURS >= 96
    assert MOMENT_MAX_HOPS == 5


# ═══ Origin: signs the effective reach, gates who gets it ════════════════


async def _post(app, db, *, reach: int, household_max: str | None = None):
    await db.enqueue(
        "UPDATE users SET preferences_json=? WHERE user_id='u-anna'",
        (f'{{"moments": {{"reach": {reach}}}}}',),
    )
    if household_max is not None:
        await app[instance_config_repo_key].set("moments.reach_max", household_max)
    return await app[moment_service_key].create_moment(
        author_user_id="u-anna", content="hi"
    )


@pytest.mark.parametrize(
    ("reach", "mark", "targets"),
    [
        (1, {NO_RELAY_FIELD: True}, {ORIGIN, RELAY, OLD38, OLD95, NEW}),
        (2, {REACH_FIELD: 2}, {ORIGIN, RELAY, NEW}),
        (3, {}, {ORIGIN, RELAY, OLD38, OLD95, NEW}),
        (5, {REACH_FIELD: 5}, {ORIGIN, RELAY, OLD38, OLD95, NEW}),
    ],
)
async def test_the_origin_signs_the_reach_on_create_and_delete(
    env, reach, mark, targets
):
    app, db, sent = env
    m = await _post(app, db, reach=reach)
    await app[moment_service_key].delete_moment(m.id, actor_user_id="u-anna")
    for et in (FET.MOMENT_CREATED, FET.MOMENT_DELETED):
        copies = [(to, p) for to, t, p in sent if t is et and p["moment_id"] == m.id]
        assert {to for to, _ in copies} == targets
        for _to, p in copies:
            assert {k: p[k] for k in (NO_RELAY_FIELD, REACH_FIELD) if k in p} == mark


async def test_the_household_maximum_caps_the_persons_reach(env):
    app, db, sent = env
    m = await _post(app, db, reach=5, household_max="2")
    assert m.reach == 2
    payloads = [p for _to, _t, p in sent if p.get("moment_id") == m.id]
    assert payloads and all(p[REACH_FIELD] == 2 for p in payloads)


async def test_a_protected_accounts_create_and_delete_are_no_relay(env):
    """The leak on main: a protected account's delete went unmarked and was
    relayed 3 hops, leaving the child's id as a tombstone there."""
    app, db, sent = env
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    m = await _post(app, db, reach=5)
    assert m.reach == 1
    await app[moment_service_key].delete_moment(m.id, actor_user_id="u-anna")
    copies = [p for _to, _t, p in sent if p.get("moment_id") == m.id]
    assert {t for _to, t, p in sent if p.get("moment_id") == m.id} == {
        FET.MOMENT_CREATED,
        FET.MOMENT_DELETED,
    }
    assert copies and all(p[NO_RELAY_FIELD] is True for p in copies)
    assert all(REACH_FIELD not in p for p in copies)


# ═══ Receiver + relay: reach honoured at each hop ════════════════════════

# (reach, hop claimed, sent by the origin) → (stored at hop, forwarded at hop)
# Relays stop at ``min(reach, MOMENT_MAX_HOPS)`` (the cap is 5); hops 4–5 go
# only to a household that looks that far — here NEW (decision C).
_HOPS = [
    (1, 1, True, 1, None),
    (2, 1, True, 1, 2),
    (2, 2, False, 2, None),
    (3, 1, True, 1, 2),
    (3, 2, False, 2, 3),
    (3, 3, False, 3, None),
    (4, 1, True, 1, 2),
    (4, 2, False, 2, 3),
    (4, 3, False, 3, 4),
    (4, 4, False, 4, None),
    (5, 1, True, 1, 2),
    (5, 2, False, 2, 3),
    (5, 3, False, 3, 4),
    (5, 4, False, 4, 5),
    (5, 5, False, 5, None),
]


@pytest.mark.parametrize(("reach", "hop", "direct", "stored", "forwarded"), _HOPS)
async def test_reach_is_honoured_at_each_hop(
    env, reach, hop, direct, stored, forwarded
):
    app, db, sent = env
    # Anna looks 5 hops, so this household keeps a hop-4–5 copy too.
    await db.enqueue(
        'UPDATE users SET preferences_json=\'{"moments": {"max_hops": 5}}\''
    )
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-hop", hop=hop, reach=reach),
        from_instance=ORIGIN if direct else RELAY,
    )
    row = await _row(db, "m-hop")
    assert row is not None and row["hop_count"] == stored
    assert row["reach"] == reach
    hops = {h for _to, h in _relayed(sent, "m-hop")}
    assert hops == (set() if forwarded is None else {forwarded})
    if forwarded is not None and forwarded > 3:
        # Only toward who looks that far (decision C).
        assert {to for to, _h in _relayed(sent, "m-hop")} == {NEW}


async def test_the_relay_clamp(env):
    """A relayed copy claiming hop 1 is stored at hop 2 and passed on as
    hop 3 — never as if it came from the origin."""
    app, db, sent = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-clamp", hop=1),
        from_instance=RELAY,
    )
    assert (await _row(db, "m-clamp"))["hop_count"] == 2
    assert {h for _to, h in _relayed(sent, "m-clamp")} == {3}


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
async def test_a_copy_beyond_its_reach_is_refused(env, et):
    app, db, sent = env
    await _deliver(
        app,
        et,
        _signed(et, "m-far", hop=3, reach=2),
        from_instance=RELAY,
    )
    assert await _row(db, "m-far") is None  # neither stored nor tombstoned
    assert _relayed(sent, "m-far") == []


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
@pytest.mark.parametrize("bad", [3, 1, 7, True, "2"])
async def test_an_invalid_reach_is_refused(env, et, bad):
    app, db, sent = env
    payload = _signed(et, "m-bad", hop=1)
    payload[REACH_FIELD] = bad
    await _deliver(app, et, payload, from_instance=ORIGIN)
    assert await _row(db, "m-bad") is None
    assert _relayed(sent, "m-bad") == []


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda p: p.pop(REACH_FIELD), id="strip"),
        pytest.param(lambda p: p.update({REACH_FIELD: 5}), id="raise"),
        pytest.param(lambda p: p.update({REACH_FIELD: 4}), id="change"),
    ],
)
async def test_a_relay_cannot_strip_or_raise_reach(env, et, tamper):
    app, db, sent = env
    payload = _signed(et, "m-tamper", hop=2, reach=2)
    tamper(payload)
    await _deliver(app, et, payload, from_instance=RELAY)
    assert await _row(db, "m-tamper") is None
    assert _relayed(sent, "m-tamper") == []


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
async def test_a_relay_cannot_add_reach(env, et):
    app, db, sent = env
    payload = _signed(et, "m-add", hop=2)
    payload[REACH_FIELD] = 5
    await _deliver(app, et, payload, from_instance=RELAY)
    assert await _row(db, "m-add") is None
    assert _relayed(sent, "m-add") == []


@pytest.mark.parametrize(
    ("reach", "targets"),
    [(3, {OLD38, OLD95, NEW, RELAY}), (4, {NEW, RELAY}), (5, {NEW, RELAY})],
)
async def test_a_relayed_reach_carrying_copy_goes_only_to_v96(env, reach, targets):
    """Reach 4–5 goes from the origin to every paired household, but a
    relayed copy carrying ``reach`` only to v_96 ones — an older household
    verifies the v1 bytes only."""
    app, _db, sent = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-wide", hop=1, reach=reach),
        from_instance=ORIGIN,
    )
    assert {to for to, _h in _relayed(sent, "m-wide")} == targets


async def test_a_closer_copy_is_relayed_again_only_once(env):
    app, _db, sent = env
    for hop, sender in ((2, RELAY), (1, ORIGIN), (1, ORIGIN), (2, RELAY)):
        await _deliver(
            app,
            FET.MOMENT_CREATED,
            _signed(FET.MOMENT_CREATED, "m-wave", hop=hop),
            from_instance=sender,
        )
    waves = sorted({h for _to, h in _relayed(sent, "m-wave")})
    assert waves == [2, 3]
    # Two waves: the first copy and one closer one — never a third.
    per_peer = [to for to, _h in _relayed(sent, "m-wave") if to == NEW]
    assert len(per_peer) == 2


# ═══ A: a far copy of a delete never suppresses the genuine closer one ═══


def _deletes(sent, moment_id: str) -> list[tuple[str, int]]:
    return sorted(
        (to, p["hop_count"])
        for to, et, p in sent
        if et is FET.MOMENT_DELETED and p.get("moment_id") == moment_id
    )


@pytest.mark.parametrize(("reach", "far_hop"), [(2, 1), (3, 3), (5, 5)])
async def test_a_far_delete_landing_first_does_not_stop_the_origins(
    env, reach, far_hop
):
    """The moment is held at hop 2; a relay's copy of its delete lands first
    at a hop it can't be relayed from (reach 2 claiming 1 → clamped 2; 3 at
    3; 5 at 5 — beyond the cap). The origin's own delete must still travel
    on — households downstream hold the moment too."""
    app, db, sent = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-del", hop=2, reach=reach),
        from_instance=RELAY,
    )
    sent.clear()
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-del", hop=far_hop, reach=reach),
        from_instance=RELAY,
    )
    assert _deletes(sent, "m-del") == []
    assert (await _row(db, "m-del"))["deleted_at"] is not None
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-del", hop=1, reach=reach),
        from_instance=ORIGIN,
    )
    relayed = _deletes(sent, "m-del")
    assert relayed and {h for _to, h in relayed} == {2}
    assert (await _row(db, "m-del"))["hop_count"] == 1
    # A replay — the origin's again, or a far one — stops here.
    sent.clear()
    for hop, sender in ((1, ORIGIN), (far_hop, RELAY)):
        await _deliver(
            app,
            FET.MOMENT_DELETED,
            _signed(FET.MOMENT_DELETED, "m-del", hop=hop, reach=reach),
            from_instance=sender,
        )
    assert _deletes(sent, "m-del") == []


@pytest.mark.parametrize(
    ("held_at", "first_hop", "first_from", "then_hop", "then_from"),
    [
        # Held at hop 2: a hop-3 copy first, then the genuine hop-2 one.
        (2, 3, RELAY, 2, RELAY),
        # Held at hop 1: a relayed copy first, then the origin's own.
        (1, 2, RELAY, 1, ORIGIN),
    ],
)
async def test_a_holder_relays_the_genuine_delete_after_a_far_one(
    env, held_at, first_hop, first_from, then_hop, then_from
):
    """The normal case: the moment is held here; a far copy of its delete
    (or a relayed one, ahead of the origin's own) lands first and wipes it.
    The closer delete arriving next must still travel on."""
    app, _db, sent = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-held", hop=held_at),
        from_instance=ORIGIN if held_at == 1 else RELAY,
    )
    sent.clear()
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-held", hop=first_hop),
        from_instance=first_from,
    )
    first = _deletes(sent, "m-held")
    sent.clear()
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-held", hop=then_hop),
        from_instance=then_from,
    )
    assert {h for _to, h in _deletes(sent, "m-held")} == {then_hop + 1}
    assert first == [] or {h for _to, h in first} == {first_hop + 1}


async def test_a_delete_never_stored_first_from_far_then_from_the_origin(env):
    """The same when no moment was held: the far copy records a tombstone at
    its hop, the origin's closer copy lowers it and travels on."""
    app, db, sent = env
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-tomb", hop=3),
        from_instance=RELAY,
    )
    assert (await _row(db, "m-tomb"))["hop_count"] == 3
    assert _deletes(sent, "m-tomb") == []
    await _deliver(
        app,
        FET.MOMENT_DELETED,
        _signed(FET.MOMENT_DELETED, "m-tomb", hop=1),
        from_instance=ORIGIN,
    )
    assert {h for _to, h in _deletes(sent, "m-tomb")} == {2}


# ═══ B: a delete follows the row, not today's protection ═════════════════


async def test_a_moment_posted_before_protection_is_deleted_as_far_as_it_went(env):
    app, db, sent = env
    m = await _post(app, db, reach=3)
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    await app[moment_service_key].delete_moment(m.id, actor_user_id="u-anna")
    deletes = [
        (to, p)
        for to, et, p in sent
        if et is FET.MOMENT_DELETED and p["moment_id"] == m.id
    ]
    assert {to for to, _p in deletes} == {ORIGIN, RELAY, OLD38, OLD95, NEW}
    assert all(NO_RELAY_FIELD not in p and REACH_FIELD not in p for _to, p in deletes)


# ═══ C: hops 4–5 only toward who looks (``moment_look_hops``) ═════════════


def _caps(**extra) -> dict:
    return {"proto_version": OURS, **extra}


def _look_of(app, instance_id: str) -> int | None:
    return app[federation_service_key].peer_moment_look_hops(instance_id)


async def test_a_peers_look_is_kept_and_bounds_hops_4_and_5(env):
    """A paired household says how far its people look; a hop-4 copy then
    goes to it — and to nobody who has not said."""
    app, _db, sent = env
    await _deliver(
        app,
        FET.INSTANCE_CAPABILITIES_UPDATED,
        _caps(moment_look_hops=4),
        from_instance=RELAY,
    )
    assert _look_of(app, RELAY) == 4
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-look", hop=3, reach=5),
        from_instance=NEW,
    )
    assert _relayed(sent, "m-look") == [(RELAY, 4)]
    # Hop 5: RELAY looks only 4 — nobody (NEW sent it).
    sent.clear()
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-look5", hop=4, reach=5),
        from_instance=NEW,
    )
    assert _relayed(sent, "m-look5") == []


@pytest.mark.parametrize("bad", [0, 6, True, "4", 4.0, None, [4]])
async def test_an_invalid_look_is_ignored_and_the_old_one_kept(env, bad):
    app, _db, _sent = env
    await _deliver(
        app,
        FET.INSTANCE_CAPABILITIES_UPDATED,
        _caps(moment_look_hops=bad),
        from_instance=NEW,
    )
    assert _look_of(app, NEW) == 5
    await _deliver(app, FET.INSTANCE_CAPABILITIES_UPDATED, _caps(), from_instance=NEW)
    assert _look_of(app, NEW) == 5


def _look_sends(sent) -> dict[str, int | None]:
    return {
        to: p.get("moment_look_hops")
        for to, et, p in sent
        if et is FET.INSTANCE_CAPABILITIES_UPDATED
    }


async def test_this_household_advertises_its_look_and_re_sends_it_on_change(env):
    """Startup carries the look to every paired household; a change (here a
    person looking further) goes to the v_96 ones only, and only when the
    number changes — a protected account never counts."""
    app, db, sent = env
    await app[capabilities_outbound_key].publish()
    looks = _look_sends(sent)
    assert set(looks) == {ORIGIN, RELAY, OLD38, OLD95, NEW}
    assert set(looks.values()) == {3}
    sent.clear()
    users = app[user_service_key]
    await users.patch_preferences("anna", {"moments": {"max_hops": 5}})
    assert _look_sends(sent) == {ORIGIN: 5, RELAY: 5, NEW: 5}
    sent.clear()
    await users.patch_preferences("anna", {"moments": {"max_hops": 5}, "x": 1})
    await users.patch_preferences("anna", {"locale": "de"})
    assert _look_sends(sent) == {}
    # Anna protected: nobody here looks past hop 1 — an admin counts (3).
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('pa', 'u-pa', 'Pa', 1)"
    )
    await app[child_protection_service_key].enable_protection(
        minor_username="anna", declared_age=10, actor_user_id="u-pa"
    )
    assert _look_sends(sent) == {ORIGIN: 3, RELAY: 3, NEW: 3}


async def test_the_look_never_goes_to_a_household_seated_from_an_invite_link(env):
    """One number, to paired households only — never to a household we only
    share a space with (nor to a GFS: the field rides only this event)."""
    app, db, sent = env
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version, relay_via)"
        " VALUES('seat', 'Seat', ?, 'k1', 'k2', '', 'wh-seat', 'confirmed',"
        " 'space_session', 96, 'https://gfs.example')",
        (generate_identity_keypair().public_key.hex(),),
    )
    await app[capabilities_outbound_key].publish()
    caps = {to: p for to, et, p in sent if et is FET.INSTANCE_CAPABILITIES_UPDATED}
    assert "seat" in caps and "moment_look_hops" not in caps["seat"]
    assert caps[NEW]["moment_look_hops"] == 3


async def test_an_upgraded_peer_is_told_how_far_we_look(env):
    """A household that just reached v_96 never kept the look we sent while
    it was older — it is told again, once, right away."""
    app, _db, sent = env
    await _deliver(app, FET.INSTANCE_CAPABILITIES_UPDATED, _caps(), from_instance=OLD95)
    assert _look_sends(sent) == {OLD95: 3}


async def test_after_our_restart_the_look_is_re_learned_and_hops_4_5_resume(env):
    """The look is kept in memory only. After our restart (memory empty) no
    hop-4 copy goes out; the startup capabilities resync asks every v_96
    peer — not the older ones — and its answer brings hop-4 copies back."""
    app, _db, sent = env
    fed = app[federation_service_key]
    fed._moment_looks.clear()  # our restart
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-before", hop=3, reach=5),
        from_instance=RELAY,
    )
    assert _relayed(sent, "m-before") == []
    sent.clear()
    await request_capability_resync(
        federation=fed,
        federation_repo=app[federation_repo_key],
        identity_repo=app[federation_repo_key],
    )
    asked = {
        to
        for to, et, p in sent
        if et is FET.INSTANCE_RESYNC_REQUEST and p == {"scope": "capabilities"}
    }
    assert asked == {ORIGIN, RELAY, NEW}
    # NEW answers with its capabilities, which carry its look.
    await _deliver(
        app,
        FET.INSTANCE_CAPABILITIES_UPDATED,
        _caps(moment_look_hops=5),
        from_instance=NEW,
    )
    sent.clear()
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-after", hop=3, reach=5),
        from_instance=RELAY,
    )
    assert _relayed(sent, "m-after") == [(NEW, 4)]


async def test_a_capabilities_resync_request_is_answered_with_our_look(env):
    """The answer to the startup resync (``INSTANCE_RESYNC_REQUEST`` scope
    ``capabilities``) is our capabilities — carrying how far we look, so the
    asking household re-learns it."""
    app, _db, sent = env
    await _deliver(
        app,
        FET.INSTANCE_RESYNC_REQUEST,
        {"scope": "capabilities"},
        from_instance=NEW,
    )
    assert _look_sends(sent) == {NEW: 3}


async def test_an_unpaired_household_is_forgotten_with_its_look(env):
    """An unpair drops the kept look with the pairing."""
    app, _db, _sent = env
    fed = app[federation_service_key]
    assert fed.peer_moment_look_hops(NEW) == 5
    await app[peer_unpair_service_key].purge(NEW)
    assert fed.peer_moment_look_hops(NEW) is None
