"""Tests for socialhome.repositories.federation_repo."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.federation import (
    GfsConnection,
    InstanceSource,
    PairingSession,
    PairingStatus,
    PeerGfsRoute,
    RemoteInstance,
)
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a federation repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.fed_repo = SqliteFederationRepo(db)
    yield e
    await db.shutdown()


async def test_federation_pairing_lifecycle(env):
    """Create, read, update, then delete a pairing session."""
    now = datetime.now(timezone.utc).isoformat()
    session = PairingSession(
        token="tok-abc",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-id",
        own_local_inbox_id="own-id",
        issued_at=now,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)

    got = await env.fed_repo.get_pairing("tok-abc")
    assert got is not None
    assert got.token == "tok-abc"
    assert got.status == PairingStatus.PENDING_SENT

    updated_session = PairingSession(
        token="tok-abc",
        own_identity_pk=session.own_identity_pk,
        own_dh_pk=session.own_dh_pk,
        own_dh_sk=session.own_dh_sk,
        inbox_url=session.inbox_url,
        own_local_inbox_id=session.own_local_inbox_id,
        peer_identity_pk="dd" * 32,
        peer_dh_pk="ee" * 32,
        peer_inbox_url="https://peer/inbox",
        issued_at=now,
        expires_at=session.expires_at,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.update_pairing(updated_session)
    refreshed = await env.fed_repo.get_pairing("tok-abc")
    assert refreshed.status == PairingStatus.PENDING_RECEIVED
    assert refreshed.peer_inbox_url == "https://peer/inbox"

    await env.fed_repo.delete_pairing("tok-abc")
    assert await env.fed_repo.get_pairing("tok-abc") is None


async def test_cleanup_expired_pairings_no_rows(env):
    """Empty table → returns 0, no side effects."""
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 0


async def test_cleanup_expired_pairings_keeps_fresh_rows(env):
    """A session whose ``expires_at`` is in the future is left alone."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    session = PairingSession(
        token="tok-fresh",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-fresh",
        own_local_inbox_id="own-fresh",
        issued_at=datetime.now(timezone.utc).isoformat(),
        expires_at=future,
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 0
    assert await env.fed_repo.get_pairing("tok-fresh") is not None


async def test_cleanup_expired_pairings_deletes_session_and_orphan_instance(env):
    """An expired session plus its PENDING_RECEIVED orphan instance both
    get pruned. A CONFIRMED instance sharing the local_inbox_id (should
    never happen — defensive guard) is left alone."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-expired",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-stale",
        own_local_inbox_id="own-stale",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.create_pairing(session)
    orphan = RemoteInstance(
        id="peer-stale",
        display_name="Stale",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://stale/wh",
        local_inbox_id="own-stale",
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.save_instance(orphan)

    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    assert await env.fed_repo.get_pairing("tok-expired") is None
    assert await env.fed_repo.get_instance("peer-stale") is None


async def test_cleanup_expired_pairings_preserves_confirmed_instance(env):
    """Defensive: even if an expired session and a CONFIRMED instance
    share a local_inbox_id, the CONFIRMED row stays. (Real flows
    delete the session before flipping the instance to CONFIRMED, so
    this scenario shouldn't arise — but the status filter is the belt
    that protects against a future bug.)"""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-x",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-confirmed",
        own_local_inbox_id="own-confirmed",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.create_pairing(session)
    inst = RemoteInstance(
        id="peer-confirmed",
        display_name="Real",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://real/wh",
        local_inbox_id="own-confirmed",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)

    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    # Confirmed row survives.
    assert await env.fed_repo.get_instance("peer-confirmed") is not None


async def test_cleanup_expired_pairings_handles_sqlite_naive_timestamp(env):
    """A row whose ``expires_at`` was written via SQLite's
    ``datetime('now', ...)`` (the ``"YYYY-MM-DD HH:MM:SS"`` shape, no
    ``T`` separator, no ``+00:00`` suffix) is still pruned correctly.

    The Python codepaths in the coordinator emit
    ``datetime.now(timezone.utc).isoformat()`` (with ``T`` and
    timezone), but the cleanup SQL has to accept both shapes because
    SQLite's bare ``datetime('now')`` will be a future writer's
    natural choice and the type difference is otherwise invisible.
    """
    # Insert via raw SQL so the row's ``expires_at`` is in SQLite's
    # naive shape rather than the Python ISO format the
    # :meth:`create_pairing` path uses.
    await env.db.enqueue(
        """
        INSERT INTO pending_pairings(
            token, own_identity_pk, own_dh_pk, own_dh_sk,
            inbox_url, own_local_inbox_id,
            status, issued_at, expires_at
        ) VALUES(?,?,?,?,?,?,?,
                 datetime('now', '-5 minutes'),
                 datetime('now', '-1 minute'))
        """,
        (
            "tok-naive",
            "aa" * 32,
            "bb" * 32,
            "cc" * 32,
            "https://local/inbox/own-naive",
            "own-naive",
            PairingStatus.PENDING_SENT.value,
        ),
    )

    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    assert await env.fed_repo.get_pairing("tok-naive") is None


async def test_cleanup_expired_pairings_handles_session_without_instance(env):
    """``initiate()`` creates a session but no RemoteInstance row. The
    cleanup must still prune the orphan session even with no peer
    instance to delete."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-initiate",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-initiate",
        own_local_inbox_id="own-initiate",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    assert await env.fed_repo.get_pairing("tok-initiate") is None


async def test_federation_replay_cache(env):
    """Insert replay IDs and confirm they appear in load_replay_cache; prune works."""
    await env.fed_repo.insert_replay_id("msg-001")
    await env.fed_repo.insert_replay_id("msg-002")

    entries = await env.fed_repo.load_replay_cache(within_hours=1)
    msg_ids = {e[0] for e in entries}
    assert "msg-001" in msg_ids
    assert "msg-002" in msg_ids

    await env.fed_repo.insert_replay_id("msg-001")

    yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    removed = await env.fed_repo.prune_replay_cache(yesterday)
    assert removed == 0

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    removed_all = await env.fed_repo.prune_replay_cache(future)
    assert removed_all >= 2


async def test_federation_instance_filtering(env):
    """Save two instances, filter by status, mark unreachable/reachable, then delete."""
    inst1 = RemoteInstance(
        id="peer-001",
        display_name="Alpha",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://alpha/wh",
        local_inbox_id="wh-1",
        status=PairingStatus.CONFIRMED,
    )
    inst2 = RemoteInstance(
        id="peer-002",
        display_name="Beta",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="https://beta/wh",
        local_inbox_id="wh-2",
        status=PairingStatus.UNPAIRING,
    )
    await env.fed_repo.save_instance(inst1)
    await env.fed_repo.save_instance(inst2)

    confirmed = await env.fed_repo.list_instances(status="confirmed")
    confirmed_ids = {i.id for i in confirmed}
    assert "peer-001" in confirmed_ids
    assert "peer-002" not in confirmed_ids

    await env.fed_repo.mark_unreachable("peer-001")
    got = await env.fed_repo.get_instance("peer-001")
    assert not got.is_reachable()

    await env.fed_repo.mark_reachable("peer-001")
    assert (await env.fed_repo.get_instance("peer-001")).is_reachable()

    await env.fed_repo.delete_instance("peer-002")
    assert await env.fed_repo.get_instance("peer-002") is None


async def test_get_instance_by_local_inbox_id_hit(env):
    inst = RemoteInstance(
        id="peer-aa",
        display_name="AA",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://aa/wh",
        local_inbox_id="inbox-aa",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    got = await env.fed_repo.get_instance_by_local_inbox_id("inbox-aa")
    assert got is not None
    assert got.id == "peer-aa"


async def test_get_instance_by_local_inbox_id_miss(env):
    assert await env.fed_repo.get_instance_by_local_inbox_id("nope") is None


async def test_list_instances_in_space_filters_membership_status_and_bans(env):
    """JOIN excludes non-members, non-confirmed peers, and banned peers."""
    member = RemoteInstance(
        id="peer-mem",
        display_name="Mem",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://mem/wh",
        local_inbox_id="wh-mem",
        status=PairingStatus.CONFIRMED,
    )
    outsider = RemoteInstance(
        id="peer-out",
        display_name="Out",
        remote_identity_pk="bb" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="https://out/wh",
        local_inbox_id="wh-out",
        status=PairingStatus.CONFIRMED,
    )
    pending = RemoteInstance(
        id="peer-pend",
        display_name="Pend",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="k5",
        key_remote_to_self="k6",
        remote_inbox_url="https://pend/wh",
        local_inbox_id="wh-pend",
        status=PairingStatus.PENDING_SENT,
    )
    banned = RemoteInstance(
        id="peer-ban",
        display_name="Ban",
        remote_identity_pk="dd" * 32,
        key_self_to_remote="k7",
        key_remote_to_self="k8",
        remote_inbox_url="https://ban/wh",
        local_inbox_id="wh-ban",
        status=PairingStatus.CONFIRMED,
    )
    for inst in (member, outsider, pending, banned):
        await env.fed_repo.save_instance(inst)

    # Add a space and seed membership.
    space_id = "sp-1"
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, space_type, owner_instance_id, "
        "owner_username, identity_public_key) "
        "VALUES(?,?,?,?,?,?)",
        (space_id, "Space", "household", env.iid, "owner", "00" * 32),
    )
    for iid in (member.id, pending.id, banned.id):
        await env.db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
            (space_id, iid),
        )
    await env.fed_repo.ban_instance_from_space(space_id, banned.id)

    got = await env.fed_repo.list_instances_in_space(space_id)
    got_ids = {i.id for i in got}
    assert got_ids == {member.id}


# ─── local_alias (PR A — user-set rename for cryptic peer names) ─────────


async def test_update_alias_sets_and_clears(env):
    inst = RemoteInstance(
        id="peer-alias",
        display_name="z7k63zfi",  # what the user actually sees today
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x/wh",
        local_inbox_id="inbox-alias",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    # No alias by default → effective name falls back to display_name.
    got = await env.fed_repo.get_instance("peer-alias")
    assert got.local_alias is None
    assert got.effective_display_name == "z7k63zfi"

    # Set the alias — effective name now wins.
    await env.fed_repo.update_alias("peer-alias", "Brother's house")
    got = await env.fed_repo.get_instance("peer-alias")
    assert got.local_alias == "Brother's house"
    assert got.effective_display_name == "Brother's house"

    # Clear it (None) — falls back to federated display_name again.
    await env.fed_repo.update_alias("peer-alias", None)
    got = await env.fed_repo.get_instance("peer-alias")
    assert got.local_alias is None
    assert got.effective_display_name == "z7k63zfi"


async def test_update_display_name_updates_advertised_not_alias(env):
    """``update_display_name`` sets the *advertised* federated name a peer
    re-broadcasts via INSTANCE_CAPABILITIES_UPDATED — it must NOT touch the
    local_alias, so an admin-set alias still wins in ``effective_display_name``.
    """
    inst = RemoteInstance(
        id="peer-rename",
        display_name="My Home",  # the QR-time name both sides started with
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x/wh",
        local_inbox_id="inbox-rename",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)

    # Peer renamed itself → advertised display_name updates.
    await env.fed_repo.update_display_name("peer-rename", "Casa Vizeli")
    got = await env.fed_repo.get_instance("peer-rename")
    assert got.display_name == "Casa Vizeli"
    assert got.local_alias is None
    assert got.effective_display_name == "Casa Vizeli"

    # With a local alias set, the alias still wins — update_display_name
    # only touched the advertised name, never local_alias.
    await env.fed_repo.update_alias("peer-rename", "Brother's house")
    await env.fed_repo.update_display_name("peer-rename", "Casa Nueva")
    got = await env.fed_repo.get_instance("peer-rename")
    assert got.display_name == "Casa Nueva"
    assert got.local_alias == "Brother's house"
    assert got.effective_display_name == "Brother's house"


async def test_save_instance_does_not_clobber_alias(env):
    """A subsequent ``save_instance`` (e.g. after URL_UPDATED, a
    proto_version bump, or any other handshake-side write) must NOT
    reset the user's locally-set alias to NULL. The alias is local-
    only state and lives on a separate column the upsert doesn't
    touch."""
    inst = RemoteInstance(
        id="peer-persist",
        display_name="zzz1",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x/wh",
        local_inbox_id="inbox-persist",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    await env.fed_repo.update_alias("peer-persist", "My alias")

    # Re-save the instance — e.g. URL_UPDATED would rewrite the URL.
    inst2 = RemoteInstance(
        id="peer-persist",
        display_name="zzz1",  # peer hasn't renamed itself
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x.NEW/wh",
        local_inbox_id="inbox-persist",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst2)
    got = await env.fed_repo.get_instance("peer-persist")
    assert got.local_alias == "My alias"  # preserved across re-save
    assert got.remote_inbox_url == "https://x.NEW/wh"  # URL did update


# ─── home_location (federation-map feature) ──────────────────────────


async def test_update_instance_home_writes_lat_lon(env):
    """Targeted UPDATE on the two columns; other columns untouched."""
    inst = RemoteInstance(
        id="peer-home",
        display_name="Bob",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://bob/wh",
        local_inbox_id="wh-bob",
        status=PairingStatus.CONFIRMED,
        home_lat=None,
        home_lon=None,
    )
    await env.fed_repo.save_instance(inst)

    await env.fed_repo.update_instance_home(
        "peer-home",
        latitude=52.52,
        longitude=13.40,
    )

    got = await env.fed_repo.get_instance("peer-home")
    assert got.home_lat == 52.52
    assert got.home_lon == 13.40
    assert got.display_name == "Bob"  # other columns intact


async def test_update_instance_home_truncates_to_4dp(env):
    """Inputs above 4dp precision are rounded — §25 invariant."""
    inst = RemoteInstance(
        id="peer-prec",
        display_name="Carol",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://carol/wh",
        local_inbox_id="wh-carol",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)

    await env.fed_repo.update_instance_home(
        "peer-prec",
        latitude=52.523456,
        longitude=13.401234,
    )

    got = await env.fed_repo.get_instance("peer-prec")
    assert got.home_lat == 52.5235
    assert got.home_lon == 13.4012


async def test_update_instance_home_unknown_id_is_noop(env):
    """Missing instance → silently noop (upstream pipeline already
    rejected the envelope if the sender wasn't known)."""
    await env.fed_repo.update_instance_home(
        "never-paired",
        latitude=1.0,
        longitude=2.0,
    )
    assert await env.fed_repo.get_instance("never-paired") is None


# ─── share_home (per-pair home-location sharing toggle) ──────────────


async def test_set_share_home_defaults_to_true(env):
    """Freshly saved instance has share_home=True (DB DEFAULT 1)."""
    inst = RemoteInstance(
        id="peer-sh-default",
        display_name="Dave",
        remote_identity_pk="33" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://dave/wh",
        local_inbox_id="wh-dave",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    got = await env.fed_repo.get_instance("peer-sh-default")
    assert got.share_home is True


async def test_set_share_home_round_trips(env):
    """set_share_home False then True — both values persist correctly."""
    inst = RemoteInstance(
        id="peer-sh-toggle",
        display_name="Eve",
        remote_identity_pk="44" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://eve/wh",
        local_inbox_id="wh-eve",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)

    # Disable sharing.
    await env.fed_repo.set_share_home("peer-sh-toggle", value=False)
    got = await env.fed_repo.get_instance("peer-sh-toggle")
    assert got.share_home is False
    assert got.display_name == "Eve"  # other columns untouched

    # Re-enable sharing.
    await env.fed_repo.set_share_home("peer-sh-toggle", value=True)
    got = await env.fed_repo.get_instance("peer-sh-toggle")
    assert got.share_home is True


async def test_save_instance_does_not_clobber_share_home(env):
    """A subsequent ``save_instance`` (e.g. after URL_UPDATED or a
    proto_version bump) must NOT reset the operator's share_home flag.
    The flag is local-only state and the UPSERT intentionally omits it
    from the DO UPDATE SET list."""
    inst = RemoteInstance(
        id="peer-sh-persist",
        display_name="Frank",
        remote_identity_pk="55" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://frank/wh",
        local_inbox_id="wh-frank",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    await env.fed_repo.set_share_home("peer-sh-persist", value=False)

    # Re-save the instance (e.g. URL_UPDATED rewrote the URL).
    inst2 = RemoteInstance(
        id="peer-sh-persist",
        display_name="Frank",
        remote_identity_pk="55" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://frank.NEW/wh",
        local_inbox_id="wh-frank",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst2)
    got = await env.fed_repo.get_instance("peer-sh-persist")
    assert got.share_home is False  # preserved across re-save
    assert got.remote_inbox_url == "https://frank.NEW/wh"  # URL did update


async def test_update_instance_home_with_nulls_clears_row(env):
    """update_instance_home(latitude=None, longitude=None) writes NULL to both columns."""
    inst = RemoteInstance(
        id="peer-null-home",
        display_name="NullHome",
        remote_identity_pk="66" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://nullhome/wh",
        local_inbox_id="wh-nullhome",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    # First set coords…
    await env.fed_repo.update_instance_home(
        "peer-null-home", latitude=52.52, longitude=13.405
    )
    got = await env.fed_repo.get_instance("peer-null-home")
    assert got.home_lat == 52.52
    assert got.home_lon == 13.405
    # …then clear them via None.
    await env.fed_repo.update_instance_home(
        "peer-null-home", latitude=None, longitude=None
    )
    got = await env.fed_repo.get_instance("peer-null-home")
    assert got.home_lat is None
    assert got.home_lon is None


async def test_last_proto_version_round_trips(env):
    """get/set_last_proto_version persist OURS on the singleton self-row."""
    # NULL until first recorded (the migration adds the column without backfill).
    assert await env.fed_repo.get_last_proto_version() is None

    await env.fed_repo.set_last_proto_version(19)
    assert await env.fed_repo.get_last_proto_version() == 19

    # Overwrites in place — no second row, latest value wins.
    await env.fed_repo.set_last_proto_version(20)
    assert await env.fed_repo.get_last_proto_version() == 20


async def test_set_instance_display_name_round_trips(env):
    """set_instance_display_name updates the federated identity display_name."""
    await env.fed_repo.set_instance_display_name("Casa Vizeli")
    identity = await env.fed_repo.get_local_identity()
    assert identity is not None
    assert identity["display_name"] == "Casa Vizeli"


async def test_list_social_instances_excludes_space_session_rows(env):
    """§D2b — a household we only share a space with is not a social peer.

    ``list_social_instances`` is what every non-space fan-out (DMs, the
    user roster, presence, the friends constellation, peer pickers)
    reads, so a ``space_session`` row must not appear in it while an
    ordinary confirmed pairing does.
    """
    paired = RemoteInstance(
        id="peer-social",
        display_name="Paired household",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://social/wh",
        local_inbox_id="wh-social",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    space_only = RemoteInstance(
        id="peer-space",
        display_name="Invite-link household",
        remote_identity_pk="bb" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="",
        local_inbox_id="wh-space",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
    )
    pending = RemoteInstance(
        id="peer-pending",
        display_name="Half-paired",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="k5",
        key_remote_to_self="k6",
        remote_inbox_url="https://pending/wh",
        local_inbox_id="wh-pending",
        status=PairingStatus.PENDING_SENT,
        source=InstanceSource.MANUAL,
    )
    for inst in (paired, space_only, pending):
        await env.fed_repo.save_instance(inst)

    social = await env.fed_repo.list_social_instances()
    assert [i.id for i in social] == ["peer-social"]
    # The row still exists and is still CONFIRMED — space federation
    # reads it through the space-scoped lists.
    everyone = await env.fed_repo.list_instances(status="confirmed")
    assert {i.id for i in everyone} == {"peer-social", "peer-space"}
    assert (await env.fed_repo.get_instance("peer-space")).source is (
        InstanceSource.SPACE_SESSION
    )


# ── Unpair tombstone (status='unpairing') ────────────────────────────────


def _tomb_inst(iid: str, inbox: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="ab" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url=f"https://{iid}/wh",
        local_inbox_id=inbox,
        status=PairingStatus.CONFIRMED,
    )


async def test_mark_unpairing_hides_row_and_drops_its_people(env):
    await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-t"))
    await env.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        ("u-t", "peer-t", "anna", "Anna"),
    )

    await env.fed_repo.mark_unpairing("peer-t")

    assert await env.fed_repo.get_instance("peer-t") is None
    assert await env.fed_repo.get_instance_by_local_inbox_id("wh-t") is None
    assert await env.fed_repo.list_instances() == []
    tomb = await env.fed_repo.get_instance("peer-t", include_unpairing=True)
    assert tomb is not None and tomb.status is PairingStatus.UNPAIRING
    by_inbox = await env.fed_repo.get_instance_by_local_inbox_id(
        "wh-t", include_unpairing=True
    )
    assert by_inbox is not None and by_inbox.id == "peer-t"
    # Transport columns survive (the outbox needs them) — the people don't.
    assert tomb.key_self_to_remote == "k1"
    assert tomb.remote_inbox_url == "https://peer-t/wh"
    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM remote_users WHERE instance_id='peer-t'"
        )
        == 0
    )


async def test_save_instance_replaces_a_tombstone(env):
    await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-old"))
    await env.fed_repo.mark_unpairing("peer-t")

    await env.fed_repo.save_instance(
        replace_status(_tomb_inst("peer-t", "wh-new"), PairingStatus.PENDING_RECEIVED)
    )

    got = await env.fed_repo.get_instance("peer-t")
    assert got is not None
    assert got.status is PairingStatus.PENDING_RECEIVED
    assert got.local_inbox_id == "wh-new"


async def test_save_instance_keeps_local_inbox_id_of_a_live_row(env):
    """Only a tombstone is replaced wholesale; a live row keeps its upsert."""
    await env.fed_repo.save_instance(_tomb_inst("peer-l", "wh-1"))
    await env.fed_repo.save_instance(_tomb_inst("peer-l", "wh-2"))
    got = await env.fed_repo.get_instance("peer-l")
    assert got is not None and got.local_inbox_id == "wh-1"


def replace_status(inst: RemoteInstance, status: PairingStatus) -> RemoteInstance:
    return replace(inst, status=status)


async def test_save_instance_keeps_the_tombstone_when_the_new_row_fails(env):
    """The tombstone swap is one transaction: if the new pairing cannot be
    written (here: its inbox id collides with another peer's), the
    tombstone — and with it our pending UNPAIR's keys and URL — survives."""
    await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-old"))
    await env.fed_repo.mark_unpairing("peer-t")
    await env.fed_repo.save_instance(_tomb_inst("peer-o", "wh-taken"))

    with pytest.raises(sqlite3.IntegrityError):
        await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-taken"))

    tomb = await env.fed_repo.get_instance("peer-t", include_unpairing=True)
    assert tomb is not None and tomb.status is PairingStatus.UNPAIRING
    assert tomb.local_inbox_id == "wh-old"


async def test_set_proto_version_is_a_high_water_mark(env):
    inst = RemoteInstance(
        id="peer-hw",
        display_name="HW",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x/hw",
        local_inbox_id="inbox-hw",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    await env.fed_repo.set_proto_version("peer-hw", 43)
    await env.fed_repo.set_proto_version("peer-hw", 42)
    assert (await env.fed_repo.get_instance("peer-hw")).proto_version == 43


async def test_a_re_pair_never_lowers_the_stored_proto_version(env):
    """The high-water mark holds across ``save_instance`` too: re-saving a
    paired household (a re-pair, an invite-redeem bootstrap) with a lower
    advertised version keeps the higher one. Only a deleted row (an unpair,
    which also drops the household's space seats) starts again from scratch."""
    inst = RemoteInstance(
        id="peer-rp",
        display_name="RP",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://x/rp",
        local_inbox_id="inbox-rp",
        status=PairingStatus.CONFIRMED,
        proto_version=43,
    )
    await env.fed_repo.save_instance(inst)
    await env.fed_repo.save_instance(replace(inst, proto_version=1))
    assert (await env.fed_repo.get_instance("peer-rp")).proto_version == 43
    await env.fed_repo.delete_instance("peer-rp")
    await env.fed_repo.save_instance(replace(inst, proto_version=1))
    assert (await env.fed_repo.get_instance("peer-rp")).proto_version == 1


# ── Bulk housekeeping purges like PeerUnpairService.purge ────────────────


async def _seed_side_rows(db, instance_id: str) -> None:
    """One row per table ``PeerUnpairService.purge`` clears, addressed to
    (or announced by) ``instance_id``. Raw SQL, past the INSERT guards: the
    point is what the purge leaves behind, whatever queued it."""
    await db.enqueue(
        "INSERT OR IGNORE INTO conversations(id, type, created_at)"
        " VALUES('conv-p', 'dm', datetime('now'))"
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO conversation_messages(id, conversation_id,"
        " sender_user_id, content, type, created_at)"
        " VALUES('m-p', 'conv-p', 'u', '', 'image', datetime('now'))"
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id,"
        " owner_username, identity_public_key, space_type, join_mode)"
        " VALUES('sp-p', 'S', 'x', 'o', ?, 'household', 'invite_only')",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO federation_outbox(id, instance_id, event_type, payload_json)"
        " VALUES(?, ?, 'unpair', '{}')",
        (f"msg-{instance_id}", instance_id),
    )
    await db.enqueue(
        "INSERT INTO dm_media_outbox(blob_id, message_id, target_instance_id,"
        " bytes_path) VALUES('b', 'm-p', ?, '/x')",
        (instance_id,),
    )
    await db.enqueue(
        "INSERT INTO space_media_outbox(blob_id, space_id, correlation_id,"
        " target_instance_id, bytes_path) VALUES('b', 'sp-p', 'c', ?, '/x')",
        (instance_id,),
    )
    await db.enqueue(
        "INSERT INTO network_discovery(instance_id, discovered_via)"
        " VALUES('far-away', ?)",
        (instance_id,),
    )


async def _side_row_counts(db, instance_id: str) -> tuple[int, int, int, int]:
    return (
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox WHERE instance_id=?",
            (instance_id,),
        ),
        await db.fetchval(
            "SELECT COUNT(*) FROM dm_media_outbox WHERE target_instance_id=?",
            (instance_id,),
        ),
        await db.fetchval(
            "SELECT COUNT(*) FROM space_media_outbox WHERE target_instance_id=?",
            (instance_id,),
        ),
        await db.fetchval(
            "SELECT COUNT(*) FROM network_discovery WHERE discovered_via=?",
            (instance_id,),
        ),
    )


async def test_cleanup_expired_pairings_purges_what_the_half_pair_queued(env):
    """An expired half-paired row goes like any other removed pairing: its
    outbox envelopes, DM / space media and announced mesh hints go in the
    same transaction. A confirmed peer's rows are untouched."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await env.fed_repo.create_pairing(
        PairingSession(
            token="tok-p",
            own_identity_pk="aa" * 32,
            own_dh_pk="bb" * 32,
            own_dh_sk="cc" * 32,
            inbox_url="https://local/inbox/own-p",
            own_local_inbox_id="own-p",
            issued_at=past,
            expires_at=past,
            status=PairingStatus.PENDING_RECEIVED,
        )
    )
    await env.fed_repo.save_instance(
        replace_status(_tomb_inst("peer-half", "own-p"), PairingStatus.PENDING_SENT)
    )
    await env.fed_repo.save_instance(_tomb_inst("peer-live", "own-live"))
    await _seed_side_rows(env.db, "peer-half")
    await _seed_side_rows(env.db, "peer-live")

    assert await env.fed_repo.cleanup_expired_pairings() == 1

    assert await env.fed_repo.get_instance("peer-half") is None
    assert await _side_row_counts(env.db, "peer-half") == (0, 0, 0, 0)
    assert await env.fed_repo.get_instance("peer-live") is not None
    assert await _side_row_counts(env.db, "peer-live") == (1, 1, 1, 1)


async def test_save_instance_replacing_a_tombstone_purges_its_side_rows(env):
    """A re-pair replaces the tombstone the way ``PeerUnpairService.purge``
    removes a row: its stale UNPAIR (sealed for the dead pairing), its
    media and the mesh hints it announced go with it, in the same
    transaction as the new row."""
    await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-old"))
    await env.fed_repo.mark_unpairing("peer-t")
    await _seed_side_rows(env.db, "peer-t")
    await _seed_side_rows(env.db, "peer-other")

    await env.fed_repo.save_instance(
        replace_status(_tomb_inst("peer-t", "wh-new"), PairingStatus.PENDING_RECEIVED)
    )

    assert await _side_row_counts(env.db, "peer-t") == (0, 0, 0, 0)
    assert await _side_row_counts(env.db, "peer-other") == (1, 1, 1, 1)


async def test_save_instance_of_a_live_row_keeps_its_side_rows(env):
    """Only a replaced tombstone purges: re-saving a live pairing (a URL
    update, a confirm) never drops what is queued for it."""
    await env.fed_repo.save_instance(_tomb_inst("peer-l", "wh-l"))
    await _seed_side_rows(env.db, "peer-l")

    await env.fed_repo.save_instance(_tomb_inst("peer-l", "wh-l"))

    assert await _side_row_counts(env.db, "peer-l") == (1, 1, 1, 1)


async def test_failed_tombstone_replacement_keeps_its_side_rows(env):
    """The purge rides the replacement's transaction: when the new row
    cannot be written, the tombstone keeps its queued UNPAIR."""
    await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-old"))
    await env.fed_repo.mark_unpairing("peer-t")
    await env.fed_repo.save_instance(_tomb_inst("peer-o", "wh-taken"))
    await _seed_side_rows(env.db, "peer-t")

    with pytest.raises(sqlite3.IntegrityError):
        await env.fed_repo.save_instance(_tomb_inst("peer-t", "wh-taken"))

    assert await _side_row_counts(env.db, "peer-t") == (1, 1, 1, 1)


# ── Mesh-only member version claims (migration 0078) ──────────────────────


async def _seat(env, space_id: str, instance_id: str) -> None:
    await env.db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
        (space_id, instance_id),
    )


async def test_member_version_unknown_household_records_nothing(env):
    """A claim never creates membership: no ``space_instances`` row → None."""
    assert await env.fed_repo.record_space_member_version("d", 51, "ab" * 32) is None
    assert await env.fed_repo.get_space_member_version("d") is None
    rows = await env.db.fetchall("SELECT * FROM space_instances")
    assert rows == []


async def test_member_version_recorded_on_every_seat_of_the_household(env):
    await _seat(env, "sp1", "d")
    await _seat(env, "sp2", "d")
    await _seat(env, "sp1", "other")
    assert await env.fed_repo.record_space_member_version("d", 51, "ab" * 32) == 0
    assert await env.fed_repo.get_space_member_version("d") == (51, "ab" * 32)
    assert await env.fed_repo.get_space_member_version("other") == (0, None)
    rows = await env.db.fetchall(
        "SELECT space_id, proto_version FROM space_instances WHERE instance_id='d'"
    )
    assert sorted((r[0], r[1]) for r in rows) == [("sp1", 51), ("sp2", 51)]


async def test_member_version_is_a_high_water_mark(env):
    await _seat(env, "sp1", "d")
    await env.fed_repo.record_space_member_version("d", 51, "ab" * 32)
    assert await env.fed_repo.record_space_member_version("d", 40, "ab" * 32) == 51
    assert await env.fed_repo.get_space_member_version("d") == (51, "ab" * 32)
    assert await env.fed_repo.record_space_member_version("d", 52, "ab" * 32) == 51
    assert await env.fed_repo.get_space_member_version("d") == (52, "ab" * 32)


async def test_member_version_survives_a_new_seat(env):
    """A later seat (NULL columns) doesn't hide the household's claim."""
    await _seat(env, "sp1", "d")
    await env.fed_repo.record_space_member_version("d", 51, "ab" * 32)
    await _seat(env, "sp2", "d")
    assert await env.fed_repo.get_space_member_version("d") == (51, "ab" * 32)


# ─── GFS relay opt-in + routes (migration 0081) ──────────────────────────


def _peer(peer_id: str = "peer-gfs", **kw) -> RemoteInstance:
    base = dict(
        id=peer_id,
        display_name="Peer",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="",
        local_inbox_id=f"inbox-{peer_id}",
        source=InstanceSource.MANUAL,
    )
    base.update(kw)
    return RemoteInstance(**base)


async def _gfs(env, gfs_id: str = "gfs-1") -> None:
    await SqliteGfsConnectionRepo(env.db).save(
        GfsConnection(
            id=gfs_id,
            gfs_instance_id=f"gi-{gfs_id}",
            display_name="GFS",
            public_key="pk",
            inbox_url=f"https://{gfs_id}.example",
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        ),
    )


async def test_gfs_relay_defaults_off(env):
    await env.fed_repo.save_instance(_peer())
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.gfs_relay is False


async def test_gfs_relay_round_trips_on_insert(env):
    await env.fed_repo.save_instance(_peer(gfs_relay=True))
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.gfs_relay is True


async def test_set_gfs_relay_toggles(env):
    await env.fed_repo.save_instance(_peer())
    await env.fed_repo.set_gfs_relay("peer-gfs", enabled=True)
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.gfs_relay is True
    await env.fed_repo.set_gfs_relay("peer-gfs", enabled=False)
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.gfs_relay is False


async def test_save_instance_does_not_clobber_the_gfs_relay_opt_in(env):
    """A rebuild-and-save (the pairing confirm does exactly this) must not
    silently switch the relay off — the opt-in has its own setter."""
    await env.fed_repo.save_instance(_peer())
    await env.fed_repo.set_gfs_relay("peer-gfs", enabled=True)
    await env.fed_repo.save_instance(_peer(display_name="Renamed"))
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None
    assert got.display_name == "Renamed"
    assert got.gfs_relay is True


async def test_set_remote_keywrap_pk(env):
    await env.fed_repo.save_instance(_peer())
    await env.fed_repo.set_remote_keywrap_pk("peer-gfs", "cc" * 32)
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.remote_keywrap_pk == "cc" * 32


async def test_gfs_route_upsert_list_delete(env):
    await env.fed_repo.save_instance(_peer())
    await _gfs(env, "gfs-1")
    await _gfs(env, "gfs-2")
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-2", now="t1")
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-1", now="t2")

    routes = await env.fed_repo.list_gfs_routes("peer-gfs")
    assert routes == [
        PeerGfsRoute("peer-gfs", "gfs-2", confirmed_at="t1", last_ack_at="t1"),
        PeerGfsRoute("peer-gfs", "gfs-1", confirmed_at="t2", last_ack_at="t2"),
    ]

    # A re-ack moves only ``last_ack_at``.
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-2", now="t9")
    routes = await env.fed_repo.list_gfs_routes("peer-gfs")
    assert routes[0] == PeerGfsRoute(
        "peer-gfs", "gfs-2", confirmed_at="t1", last_ack_at="t9"
    )

    await env.fed_repo.delete_gfs_route("peer-gfs", "gfs-2")
    assert [
        r.gfs_connection_id for r in await env.fed_repo.list_gfs_routes("peer-gfs")
    ] == ["gfs-1"]


async def test_gfs_routes_list_is_per_peer(env):
    await env.fed_repo.save_instance(_peer("p1"))
    await env.fed_repo.save_instance(_peer("p2"))
    await _gfs(env)
    await env.fed_repo.upsert_gfs_route("p1", "gfs-1", now="t1")
    assert await env.fed_repo.list_gfs_routes("p2") == []


async def test_delete_gfs_routes_older_than(env):
    await env.fed_repo.save_instance(_peer())
    await _gfs(env, "gfs-1")
    await _gfs(env, "gfs-2")
    old = "2026-01-01T00:00:00+00:00"
    fresh = "2026-06-01T00:00:00+00:00"
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-1", now=old)
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-2", now=fresh)

    removed = await env.fed_repo.delete_gfs_routes_older_than(
        "2026-03-01T00:00:00+00:00"
    )

    assert removed == 1
    assert [
        r.gfs_connection_id for r in await env.fed_repo.list_gfs_routes("peer-gfs")
    ] == ["gfs-2"]


async def test_gfs_routes_cascade_with_the_peer(env):
    await env.fed_repo.save_instance(_peer())
    await _gfs(env)
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-1", now="t1")
    await env.fed_repo.delete_instance("peer-gfs")
    await env.fed_repo.save_instance(_peer())
    assert await env.fed_repo.list_gfs_routes("peer-gfs") == []


async def test_gfs_routes_cascade_with_the_gfs_connection(env):
    await env.fed_repo.save_instance(_peer())
    await _gfs(env)
    await env.fed_repo.upsert_gfs_route("peer-gfs", "gfs-1", now="t1")
    await SqliteGfsConnectionRepo(env.db).delete("gfs-1")
    assert await env.fed_repo.list_gfs_routes("peer-gfs") == []


async def test_save_instance_without_a_keywrap_key_keeps_the_stored_one(env):
    """A rebuild-and-save that does not carry the key (the pairing confirm
    rebuilds the row from scratch) must not wipe it while the relay opt-in
    stays on — the relay would then have nothing to seal to."""
    await env.fed_repo.save_instance(_peer(remote_keywrap_pk="cc" * 32))
    await env.fed_repo.set_gfs_relay("peer-gfs", enabled=True)
    await env.fed_repo.save_instance(_peer(remote_keywrap_pk=None))
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None
    assert got.remote_keywrap_pk == "cc" * 32
    assert got.gfs_relay is True


async def test_save_instance_with_a_new_keywrap_key_replaces_it(env):
    """A space-session re-seat brings a freshly verified key — it wins."""
    await env.fed_repo.save_instance(
        _peer(source=InstanceSource.SPACE_SESSION, remote_keywrap_pk="cc" * 32),
    )
    await env.fed_repo.save_instance(
        _peer(source=InstanceSource.SPACE_SESSION, remote_keywrap_pk="dd" * 32),
    )
    got = await env.fed_repo.get_instance("peer-gfs")
    assert got is not None and got.remote_keywrap_pk == "dd" * 32
