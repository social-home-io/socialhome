"""Extra coverage for GFS repositories (admin + federation helpers)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace

import pytest

from socialhome.global_server.domain import (
    ClientInstance,
    ClusterNode,
    GfsAppeal,
    GfsFraudReport,
    GfsMomentFollow,
    GfsMomentKeyMail,
    GfsSpaceMedia,
    GfsSpaceMediaDemand,
    GfsSpaceMediaMemberWant,
    GfsSubscriberWithKeys,
    GfsUserRegistration,
    GlobalSpace,
)
from socialhome.global_server.repositories import (
    SqliteClusterRepo,
    SqliteGfsAdminRepo,
    SqliteGfsChannelRepo,
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
    SqliteGfsInviteRepo,
    SqliteGfsMomentFollowRepo,
    SqliteGfsMomentKeyMailboxRepo,
    SqliteGfsSpaceEpochRepo,
    SqliteGfsSpaceMediaDemandRepo,
    SqliteGfsSpaceMediaMemberWantRepo,
    SqliteGfsSpaceMediaRepo,
    SqliteGfsUserRegistrationRepo,
)


@pytest.fixture
async def fed(gfs_db):
    return SqliteGfsFederationRepo(gfs_db)


@pytest.fixture
async def admin(gfs_db):
    return SqliteGfsAdminRepo(gfs_db)


# ── Federation helpers ────────────────────────────────────────────────


async def test_list_instances_filtered_by_status(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="a",
            display_name="A",
            public_key="aa" * 32,
            status="pending",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="b",
            display_name="B",
            public_key="bb" * 32,
            status="active",
        )
    )
    active = await fed.list_instances(status="active")
    assert {x.instance_id for x in active} == {"b"}
    pending = await fed.list_instances(status="pending")
    assert {x.instance_id for x in pending} == {"a"}


async def test_upsert_instance_round_trips_keywrap_fields(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="kw",
            display_name="KW",
            public_key="aa" * 32,
            keywrap_public_key="dd" * 32,
            kem_suite="x25519",
        )
    )
    got = await fed.get_instance("kw")
    assert got is not None
    assert got.keywrap_public_key == "dd" * 32
    assert got.kem_suite == "x25519"


async def test_get_instance_legacy_row_without_keywrap_is_empty(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="legacy",
            display_name="L",
            public_key="aa" * 32,
        )
    )
    got = await fed.get_instance("legacy")
    assert got is not None
    assert got.keywrap_public_key == ""
    assert got.kem_suite == ""
    assert got.keywrap_sig == ""


async def test_upsert_instance_round_trips_keywrap_sig(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="kws",
            display_name="KWS",
            public_key="aa" * 32,
            keywrap_public_key="dd" * 32,
            kem_suite="x25519",
            keywrap_sig="c2ln",
        )
    )
    got = await fed.get_instance("kws")
    assert got is not None
    assert got.keywrap_sig == "c2ln"


async def test_set_instance_display_name_updates_only_name(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="rn",
            display_name="Old",
            public_key="cc" * 32,
            status="active",
        )
    )
    await fed.set_instance_display_name("rn", "New Name")
    inst = await fed.get_instance("rn")
    assert inst is not None
    assert inst.display_name == "New Name"
    # Other columns untouched.
    assert inst.public_key == "cc" * 32
    assert inst.status == "active"


async def test_list_spaces_for_instance(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="owner",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="s1",
            owning_instance="owner",
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="s2",
            owning_instance="owner",
            status="banned",
        )
    )
    got = await fed.list_spaces_for_instance("owner")
    assert {s.space_id for s in got} == {"s1", "s2"}


async def test_remove_subscriber_updates_count(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub",
            display_name="S",
            public_key="bb" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp",
            owning_instance="o",
            status="active",
        )
    )
    await fed.add_subscriber(space_id="sp", instance_id="sub")
    sp = await fed.get_space("sp")
    assert sp.subscriber_count == 1
    await fed.remove_subscriber(space_id="sp", instance_id="sub")
    sp = await fed.get_space("sp")
    assert sp.subscriber_count == 0


async def test_purge_subscribers_drops_every_seat_and_zeroes_the_count(fed):
    """``purge_subscribers`` is the repair the publish handler runs when a
    space turns out to be invite-only: every seat goes, the count follows,
    and the number removed comes back for the log line."""
    for iid in ("po", "ps1", "ps2"):
        await fed.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key="aa" * 32,
                status="active",
            )
        )
    await fed.upsert_space(
        GlobalSpace(space_id="sp-p", owning_instance="po", status="active")
    )
    await fed.add_subscriber(space_id="sp-p", instance_id="ps1")
    await fed.add_subscriber(space_id="sp-p", instance_id="ps2")
    assert await fed.purge_subscribers("sp-p") == 2
    assert await fed.list_subscribers("sp-p") == []
    sp = await fed.get_space("sp-p")
    assert sp is not None and sp.subscriber_count == 0
    # Idempotent: a second purge (or one on a space with no seats) is a no-op.
    assert await fed.purge_subscribers("sp-p") == 0


async def test_upsert_space_round_trips_join_mode(fed):
    """``join_mode`` persists, and an unknown value normalises to the
    fail-closed ``invite_only`` on the way in."""
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp-jm",
            owning_instance="o",
            status="active",
            join_mode="open",
        )
    )
    assert (await fed.get_space("sp-jm")).join_mode == "open"
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp-jm2",
            owning_instance="o",
            status="active",
            join_mode="whatever",
        )
    )
    assert (await fed.get_space("sp-jm2")).join_mode == "invite_only"


async def test_upsert_space_round_trips_allow_subscribers(fed):
    """The readability opt-in persists independently of ``join_mode``, and a
    row written without it (migration 0010's default) reads as False."""
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o2",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp-rd",
            owning_instance="o2",
            status="active",
            join_mode="invite_only",
            allow_subscribers=True,
        )
    )
    row = await fed.get_space("sp-rd")
    assert row.allow_subscribers is True and row.join_mode == "invite_only"
    # The dataclass default is the fail-closed one.
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp-rd2",
            owning_instance="o2",
            status="active",
            join_mode="open",
        )
    )
    row2 = await fed.get_space("sp-rd2")
    assert row2.allow_subscribers is False and row2.join_mode == "open"


async def test_list_subscribers_with_keys_joins_client_instances(fed):
    """The reconcile query JOINs subscribers × client_instances so the
    seed-holder gets each subscriber's identity + key-wrap material to seal
    the content key to."""
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub-kw",
            display_name="WithKeywrap",
            public_key="bb" * 32,
            status="active",
            keywrap_public_key="cc" * 32,
            kem_suite="x25519",
            keywrap_sig="sig-kw",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub-bare",
            display_name="NoKeywrap",
            public_key="dd" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(space_id="sp", owning_instance="o", status="active")
    )
    await fed.add_subscriber(space_id="sp", instance_id="sub-kw")
    await fed.add_subscriber(space_id="sp", instance_id="sub-bare")

    rows = await fed.list_subscribers_with_keys("sp")
    assert all(isinstance(r, GfsSubscriberWithKeys) for r in rows)
    by_id = {r.instance_id: r for r in rows}
    assert set(by_id) == {"sub-kw", "sub-bare"}

    kw = by_id["sub-kw"]
    assert kw.identity_public_key == "bb" * 32
    assert kw.keywrap_public_key == "cc" * 32
    assert kw.keywrap_sig == "sig-kw"

    # A subscriber that registered without a key-wrap key surfaces with empty
    # key-wrap fields (the seed-holder skips sealing to it).
    bare = by_id["sub-bare"]
    assert bare.identity_public_key == "dd" * 32
    assert bare.keywrap_public_key == ""
    assert bare.keywrap_sig == ""


async def test_list_subscribers_with_keys_unknown_space_is_empty(fed):
    assert await fed.list_subscribers_with_keys("nope") == []


async def _seat(fed, *, instance_id: str, status: str = "active") -> None:
    await fed.upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key="ab" * 32,
            status=status,
        )
    )
    await fed.add_subscriber(space_id="sp-get", instance_id=instance_id)


async def test_get_subscriber_returns_one_active_seat(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o", display_name="O", public_key="aa" * 32, status="active"
        )
    )
    await fed.upsert_space(
        GlobalSpace(space_id="sp-get", owning_instance="o", status="active")
    )
    await _seat(fed, instance_id="s-on")
    await _seat(fed, instance_id="s-off", status="banned")
    got = await fed.get_subscriber("sp-get", "s-on")
    assert got is not None and got.instance_id == "s-on"
    # Same semantics as list_subscribers: an inactive instance is no seat.
    assert await fed.get_subscriber("sp-get", "s-off") is None
    assert await fed.get_subscriber("sp-get", "nobody") is None
    assert await fed.get_subscriber("other", "s-on") is None


async def test_get_subscriber_is_a_primary_key_lookup(fed, gfs_db):
    """The (space_id, instance_id) PRIMARY KEY covers the point lookup — no
    scan of the space's seats, and no new index or migration."""
    plan = await gfs_db.fetchall(
        "EXPLAIN QUERY PLAN " + fed.GET_SUBSCRIBER_SQL, ("sp", "i")
    )
    details = [str(dict(r).get("detail", "")) for r in plan]
    assert details, "no query plan"
    assert not any(d.startswith("SCAN") for d in details), details
    assert any("sqlite_autoindex_space_subscribers" in d for d in details), details


# ── Admin helpers ─────────────────────────────────────────────────────


async def test_count_reports_by_reporter(admin):
    now = int(time.time())
    for i in range(3):
        await admin.save_fraud_report(
            GfsFraudReport(
                id=f"rep-{i}",
                target_type="space",
                target_id=f"t-{i}",
                category="spam",
                notes=None,
                reporter_instance_id="rep.home",
                reporter_user_id=None,
                status="pending",
                created_at=now,
            )
        )
    cnt = await admin.count_reports_by_reporter("rep.home", since=now - 60)
    assert cnt == 3


async def test_get_config_returns_none_for_unknown_key(admin):
    assert await admin.get_config("no-such-key") is None


async def test_set_config_is_idempotent(admin):
    await admin.set_config("k", "v1")
    await admin.set_config("k", "v2")
    assert await admin.get_config("k") == "v2"


async def test_admin_session_roundtrip(admin):
    await admin.create_session("t-1", expires_at=int(time.time()) + 3600)
    session = await admin.get_session("t-1")
    assert session is not None
    assert session.token == "t-1"
    await admin.delete_session("t-1")
    assert await admin.get_session("t-1") is None


async def test_admin_session_purge_expired(admin):
    await admin.create_session("old", expires_at=int(time.time()) - 1)
    await admin.create_session("fresh", expires_at=int(time.time()) + 1000)
    await admin.purge_expired_sessions(int(time.time()))
    assert await admin.get_session("old") is None
    assert await admin.get_session("fresh") is not None


async def test_appeal_persist_list_and_decide(admin):
    a = GfsAppeal(
        id="a1",
        target_type="space",
        target_id="sp",
        message="plz",
        status="pending",
        created_at=int(time.time()),
    )
    await admin.save_appeal(a)
    pending = await admin.list_appeals(status="pending")
    assert any(x.id == "a1" for x in pending)
    await admin.set_appeal_status("a1", status="lifted", decided_by="admin")
    got = await admin.get_appeal("a1")
    assert got.status == "lifted"


async def test_pair_token_single_use_and_ttl(admin):
    # Single-use + expired behaviour covered here.
    await admin.save_pair_token("tok-1", "1.2.3.4")
    assert await admin.consume_pair_token("tok-1") is True
    # Already consumed.
    assert await admin.consume_pair_token("tok-1") is False
    # Unknown token.
    assert await admin.consume_pair_token("nope") is False


async def test_pair_token_concurrent_consume_succeeds_once(admin):
    """The consume is ONE conditional UPDATE: of many concurrent consumers of
    the same token exactly one wins."""
    await admin.save_pair_token("tok-race", "1.2.3.4")
    results = await asyncio.gather(
        *[admin.consume_pair_token("tok-race") for _ in range(25)]
    )
    assert results.count(True) == 1


async def test_pair_token_expired_is_refused(admin, gfs_db):
    await admin.save_pair_token("tok-old", "1.2.3.4")
    await gfs_db.enqueue(
        "UPDATE gfs_pair_tokens SET created_at=? WHERE token=?",
        (int(time.time()) - 601, "tok-old"),
    )
    assert await admin.consume_pair_token("tok-old") is False


async def test_prune_old_pair_tokens_drops_old_keeps_recent(admin, gfs_db):
    """``prune_old_pair_tokens`` deletes tokens created before the cutoff."""
    now = int(time.time())
    old = now - 2 * 86400  # 2 days ago
    recent = now - 60  # 1 minute ago
    await gfs_db.enqueue(
        "INSERT INTO gfs_pair_tokens(token, ip, created_at) VALUES(?, ?, ?)",
        ("old-tok", "1.1.1.1", old),
    )
    await gfs_db.enqueue(
        "INSERT INTO gfs_pair_tokens(token, ip, created_at) VALUES(?, ?, ?)",
        ("recent-tok", "2.2.2.2", recent),
    )

    cutoff = now - 86400  # 24h
    deleted = await admin.prune_old_pair_tokens(cutoff)
    assert deleted == 1

    rows = await gfs_db.fetchall("SELECT token FROM gfs_pair_tokens")
    tokens = {r["token"] for r in rows}
    assert "old-tok" not in tokens  # old row pruned
    assert "recent-tok" in tokens  # recent row kept


async def test_record_login_attempt_prunes_old_rows(admin, gfs_db):
    """Prune-on-write bounds the brute-force counter table.

    An old attempt (beyond the retention window) is dropped when a new
    attempt is recorded, while recent attempts survive and still count.
    """
    # Seed an ancient attempt directly (2 days old, beyond the 24h retention).
    old = int(time.time()) - 2 * 86400
    await gfs_db.enqueue(
        "INSERT INTO admin_login_attempts(ip, attempted_at) VALUES(?, ?)",
        ("9.9.9.9", old),
    )
    # Recording a fresh attempt triggers the prune-on-write.
    await admin.record_login_attempt("1.2.3.4")

    rows = await gfs_db.fetchall("SELECT ip FROM admin_login_attempts")
    ips = {r["ip"] for r in rows}
    assert "9.9.9.9" not in ips  # old row pruned
    assert "1.2.3.4" in ips  # recent row kept
    # The recent attempt still counts within a generous window.
    recent = int(time.time()) - 3600
    assert await admin.count_failed_attempts("1.2.3.4", since=recent) == 1


async def _owner(fed, instance_id: str = "o") -> None:
    """Insert the owning instance a ``global_spaces`` row FK-references."""
    await fed.upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key="aa" * 32,
            status="active",
        )
    )


async def test_set_space_withdrawn_round_trips(fed):
    """``withdrawn`` is owner withdrawal — a targeted setter that leaves every
    other column (``status`` and the branding fields included) alone."""
    await _owner(fed)
    await fed.upsert_space(
        GlobalSpace(
            space_id="w1",
            owning_instance="o",
            name="Withdrawable",
            status="active",
            icon_url="data:image/webp;base64,AAAA",
            primary_color="#654321",
            identity_public_key="ee" * 32,
        )
    )
    sp = await fed.get_space("w1")
    assert sp is not None
    assert sp.withdrawn is False

    await fed.set_space_withdrawn("w1", True)
    sp = await fed.get_space("w1")
    assert sp is not None
    assert sp.withdrawn is True
    assert sp.status == "active"
    assert sp.icon_url == "data:image/webp;base64,AAAA"
    assert sp.primary_color == "#654321"
    assert sp.identity_public_key == "ee" * 32

    await fed.set_space_withdrawn("w1", False)
    sp = await fed.get_space("w1")
    assert sp is not None
    assert sp.withdrawn is False


async def test_list_spaces_excludes_withdrawn(fed):
    """``list_spaces`` is the public discovery read — a withdrawn space drops
    out of it (both with and without a status filter), while ``get_space``
    still returns the row (``publish_space`` needs it for the owner /
    TOFU-pin checks)."""
    await _owner(fed)
    await fed.upsert_space(
        GlobalSpace(space_id="v1", owning_instance="o", status="active")
    )
    await fed.upsert_space(
        GlobalSpace(space_id="v2", owning_instance="o", status="active")
    )
    await fed.set_space_withdrawn("v2", True)

    assert {s.space_id for s in await fed.list_spaces(status="active")} == {"v1"}
    assert {s.space_id for s in await fed.list_spaces()} == {"v1"}
    assert await fed.get_space("v2") is not None


async def test_upsert_space_clears_withdrawn(fed):
    """A full ``upsert_space`` (the owner re-publish path) carries the
    ``withdrawn`` flag, so re-publishing restores visibility."""
    await _owner(fed)
    await fed.upsert_space(
        GlobalSpace(space_id="v3", owning_instance="o", status="active")
    )
    await fed.set_space_withdrawn("v3", True)
    await fed.upsert_space(
        GlobalSpace(space_id="v3", owning_instance="o", status="active")
    )
    sp = await fed.get_space("v3")
    assert sp is not None
    assert sp.withdrawn is False


async def test_list_spaces_include_withdrawn_opt_in(fed):
    """``include_withdrawn=True`` is the moderator read — the admin console
    must keep seeing a space an owner withdrew, at every status filter,
    or a withdrawal would put the space out of reach of a ban."""
    await _owner(fed)
    await fed.upsert_space(
        GlobalSpace(space_id="m1", owning_instance="o", status="active")
    )
    await fed.upsert_space(
        GlobalSpace(space_id="m2", owning_instance="o", status="active")
    )
    await fed.set_space_withdrawn("m2", True)

    listed = await fed.list_spaces(include_withdrawn=True)
    assert {s.space_id for s in listed} == {"m1", "m2"}
    listed = await fed.list_spaces(status="active", include_withdrawn=True)
    assert {s.space_id for s in listed} == {"m1", "m2"}


async def test_upsert_space_never_clears_a_pinned_identity_key(fed):
    """The TOFU-pinned space authority key is immutable once set, enforced
    in SQL. A write carrying an empty ``identity_public_key`` (e.g. a
    cluster ``NODE_SYNC_SPACE`` rebuilt from a partial wire shape) must
    not wipe the pin and downgrade the space to owner-only relay."""
    await _owner(fed)
    await fed.upsert_space(
        GlobalSpace(
            space_id="p1",
            owning_instance="o",
            status="active",
            identity_public_key="cc" * 32,
        )
    )
    await fed.upsert_space(
        GlobalSpace(space_id="p1", owning_instance="o", status="active")
    )
    sp = await fed.get_space("p1")
    assert sp is not None
    assert sp.identity_public_key == "cc" * 32


async def test_list_subscribed_spaces_returns_only_subscriptions(fed):
    """``list_subscribed_spaces`` returns the spaces an instance SUBSCRIBES to
    (Phase-5b-d reconnect notify), not the ones it owns."""
    for iid in ("owner-x", "sub-x"):
        await fed.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key="aa" * 32,
                status="active",
            )
        )
    await fed.upsert_space(
        GlobalSpace(space_id="sx1", owning_instance="owner-x", status="active")
    )
    await fed.upsert_space(
        GlobalSpace(space_id="sx2", owning_instance="owner-x", status="active")
    )
    await fed.upsert_space(
        GlobalSpace(space_id="sx3", owning_instance="sub-x", status="active")
    )
    await fed.add_subscriber(space_id="sx1", instance_id="sub-x")
    await fed.add_subscriber(space_id="sx2", instance_id="sub-x")

    got = await fed.list_subscribed_spaces("sub-x")
    assert {s.space_id for s in got} == {"sx1", "sx2"}
    assert all(s.owning_instance == "owner-x" for s in got)


async def test_list_subscribed_spaces_empty_for_unknown_instance(fed):
    assert await fed.list_subscribed_spaces("nobody") == []


# ── Invite tokens ─────────────────────────────────────────────────────


@pytest.fixture
async def invites(gfs_db):
    return SqliteGfsInviteRepo(gfs_db)


async def _listed_space(fed, space_id: str = "inv-sp") -> None:
    await fed.upsert_instance(
        ClientInstance(
            instance_id="inv-owner",
            display_name="Owner",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id=space_id,
            owning_instance="inv-owner",
            name="Space",
            status="active",
        )
    )


async def test_create_and_get_live_round_trip(invites, fed):
    await _listed_space(fed)
    now = int(time.time())
    created = await invites.create(
        gfs_token="tok-a",
        space_id="inv-sp",
        source_instance_id="inv-owner",
        blob="YWJj",
        created_at=now,
        expires_at=now + 60,
    )
    assert created.blob == "YWJj"
    got = await invites.get_live("tok-a", now=now)
    assert got is not None
    assert got.gfs_token == "tok-a"
    assert got.space_id == "inv-sp"
    assert got.source_instance_id == "inv-owner"
    assert got.blob == "YWJj"
    assert got.expires_at == now + 60


async def test_get_live_filters_expired_rows(invites, fed):
    """An expired row is invisible between two sweeps — the reader must not
    depend on the hourly prune having run."""
    await _listed_space(fed)
    now = int(time.time())
    await invites.create(
        gfs_token="tok-old",
        space_id="inv-sp",
        source_instance_id="inv-owner",
        blob="YWJj",
        created_at=now - 120,
        expires_at=now - 1,
    )
    assert await invites.get_live("tok-old", now=now) is None


async def test_get_live_unknown_token(invites):
    assert await invites.get_live("nope", now=int(time.time())) is None


async def test_delete_and_prune(invites, fed):
    await _listed_space(fed)
    now = int(time.time())
    for token, expires in (("t1", now + 60), ("t2", now - 1), ("t3", now - 5)):
        await invites.create(
            gfs_token=token,
            space_id="inv-sp",
            source_instance_id="inv-owner",
            blob="YWJj",
            created_at=now - 10,
            expires_at=expires,
        )
    assert await invites.delete("t1") == 1
    assert await invites.delete("t1") == 0
    assert await invites.prune_expired(now) == 2


async def test_delete_for_space_drops_every_invite(invites, fed):
    await _listed_space(fed)
    await _listed_space(fed, "inv-sp2")
    now = int(time.time())
    for token, space in (("a", "inv-sp"), ("b", "inv-sp"), ("c", "inv-sp2")):
        await invites.create(
            gfs_token=token,
            space_id=space,
            source_instance_id="inv-owner",
            blob="YWJj",
            created_at=now,
            expires_at=now + 60,
        )
    assert await invites.delete_for_space("inv-sp") == 2
    assert await invites.get_live("c", now=now) is not None


def test_no_invite_statement_names_the_use_counter():
    """PRIVACY: ``uses`` / ``max_uses`` are dead columns by design — a use
    counter would make the server a record of who joined what.

    Reads the SQL the repo actually compiles (every string constant naming
    the table) rather than its prose, so the assertion can't be satisfied by
    a comment or defeated by one.
    """
    statements = [
        const
        for name in dir(SqliteGfsInviteRepo)
        if callable(getattr(SqliteGfsInviteRepo, name, None))
        for const in _string_consts(getattr(SqliteGfsInviteRepo, name))
        if "gfs_invite_tokens" in const
    ]
    assert statements, "no SQL found — did the repo move?"
    for sql in statements:
        assert "uses" not in sql, sql


def _string_consts(func) -> list[str]:
    """Every string constant in *func*, including nested code objects."""
    code = getattr(func, "__code__", None)
    if code is None:
        return []
    out: list[str] = []
    stack = [code]
    while stack:
        current = stack.pop()
        for const in current.co_consts:
            if isinstance(const, str):
                out.append(const)
            elif hasattr(const, "co_consts"):
                stack.append(const)
    return out


async def _owner_row(repo) -> None:
    await repo.upsert_instance(
        ClientInstance(
            instance_id="o",
            display_name="O",
            public_key="ee" * 32,
            status="active",
        )
    )


async def test_set_space_authority_is_compare_and_set(gfs_db):
    """v_44: the pin + cert move together, and only from the state the
    caller verified against."""
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(
        GlobalSpace(space_id="sp", owning_instance="o", identity_public_key="aa" * 32)
    )
    cert = {"key_epoch": 1}
    assert await repo.set_space_authority(
        "sp", expected_pk="aa" * 32, expected_cert=None, new_pk="bb" * 32, cert=cert
    )
    row = await repo.get_space("sp")
    assert (row.identity_public_key, row.authority_cert) == ("bb" * 32, cert)
    assert not await repo.set_space_authority(
        "sp", expected_pk="aa" * 32, expected_cert=None, new_pk="cc" * 32, cert=cert
    )
    assert (await repo.get_space("sp")).identity_public_key == "bb" * 32


async def test_upsert_space_never_moves_a_set_pin(gfs_db):
    """Only the cert-checked ``set_space_authority`` may change a pin; the
    ordinary upsert (publish refresh, cluster gossip) keeps it."""

    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    s = GlobalSpace(space_id="sp2", owning_instance="o", identity_public_key="aa" * 32)
    await repo.upsert_space(s)
    await repo.upsert_space(
        replace(s, identity_public_key="dd" * 32, authority_cert={"key_epoch": 9})
    )
    row = await repo.get_space("sp2")
    assert row.identity_public_key == "aa" * 32
    assert row.authority_cert is None


async def test_raise_authority_rotation_seq_is_a_max_merge_for_the_held_pin(gfs_db):
    """F2: a peer node's seq raises ours, never lowers it, and only while we
    pin the key it describes."""
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(
        GlobalSpace(space_id="sp", owning_instance="o", identity_public_key="aa" * 32)
    )
    await repo.raise_authority_rotation_seq("sp", pk="aa" * 32, seq=4)
    assert (await repo.get_space("sp")).authority_rotation_seq == 4
    await repo.raise_authority_rotation_seq("sp", pk="aa" * 32, seq=2)
    assert (await repo.get_space("sp")).authority_rotation_seq == 4
    await repo.raise_authority_rotation_seq("sp", pk="bb" * 32, seq=9)
    assert (await repo.get_space("sp")).authority_rotation_seq == 4


async def test_authority_rotation_seq_is_capped_at_int64_max(gfs_db):
    """A hostile cluster peer gossiping a huge seq cannot push the counter
    past 2**63 - 1 (SQLite's INTEGER range) — neither by the max-merge nor
    by a re-pin's +1 on top of it."""
    cap = 2**63 - 1
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(
        GlobalSpace(space_id="sp", owning_instance="o", identity_public_key="aa" * 32)
    )
    await repo.raise_authority_rotation_seq("sp", pk="aa" * 32, seq=2**64)
    assert (await repo.get_space("sp")).authority_rotation_seq == cap
    assert await repo.set_space_authority(
        "sp",
        expected_pk="aa" * 32,
        expected_cert=None,
        new_pk="bb" * 32,
        cert={"key_epoch": 1},
    )
    row = await repo.get_space("sp")
    assert row.authority_rotation_seq == cap
    assert isinstance(row.authority_rotation_seq, int)


# ── Space content epochs (v_49, migration 0014) ──────────────────────


async def test_space_epoch_is_none_until_one_is_learned(gfs_db):
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(GlobalSpace(space_id="sp", owning_instance="o"))
    epochs = SqliteGfsSpaceEpochRepo(gfs_db)
    assert await epochs.get("sp") is None
    assert await epochs.get("unknown") is None


async def _epochs(gfs_db, **space_kw):
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(GlobalSpace(space_id="sp", owning_instance="o", **space_kw))
    return repo, SqliteGfsSpaceEpochRepo(gfs_db)


async def test_owner_confirm_is_monotonic_and_keeps_the_previous(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    assert await epochs.confirm("sp", 3, now=100)
    state = await epochs.get("sp")
    # The first epoch confirmed gets ``epoch - 1`` as its predecessor (grace).
    assert (state.current, state.confirmed, state.previous, state.confirmed_at) == (
        3,
        3,
        2,
        100,
    )
    assert await epochs.confirm("sp", 5, now=200)
    state = await epochs.get("sp")
    assert (state.confirmed, state.previous, state.confirmed_at) == (5, 3, 200)
    # Equal or older never moves it (a replayed notice can't roll it back).
    assert not await epochs.confirm("sp", 5, now=300)
    assert not await epochs.confirm("sp", 4, now=300)
    assert (await epochs.get("sp")).confirmed == 5


async def test_a_step_raises_current_by_one_once_a_minute_never_the_floor(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    # Nothing to step from before the owner confirmed an epoch.
    assert not await epochs.step("sp", 1, now=10_000, min_interval_s=60)
    assert await epochs.get("sp") is None
    await epochs.confirm("sp", 3, now=100)
    assert not await epochs.step("sp", 5, now=10_000, min_interval_s=60)
    assert not await epochs.step("sp", 4, now=150, min_interval_s=60)
    assert await epochs.step("sp", 4, now=160, min_interval_s=60)
    assert not await epochs.step("sp", 5, now=200, min_interval_s=60)
    state = await epochs.get("sp")
    assert (state.current, state.confirmed, state.previous) == (4, 3, 2)


async def test_owner_confirm_never_lowers_current(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    await epochs.confirm("sp", 3, now=100)
    await epochs.step("sp", 4, now=200, min_interval_s=60)
    await epochs.confirm("sp", 4, now=300)
    state = await epochs.get("sp")
    assert (state.current, state.confirmed, state.previous) == (4, 4, 3)


async def test_space_epoch_survives_a_publish_refresh(gfs_db):
    repo, epochs = await _epochs(gfs_db)
    await epochs.confirm("sp", 7, now=1)
    await repo.upsert_space(
        GlobalSpace(space_id="sp", owning_instance="o", name="renamed")
    )
    assert (await epochs.get("sp")).confirmed == 7


async def test_space_epoch_is_forgotten_when_the_authority_key_is_repinned(gfs_db):
    """A revoked seed holder must not leave an inflated epoch behind."""
    repo, epochs = await _epochs(gfs_db, identity_public_key="aa" * 32)
    await epochs.confirm("sp", 2**40, now=1)
    assert await repo.set_space_authority(
        "sp",
        expected_pk="aa" * 32,
        expected_cert=None,
        new_pk="bb" * 32,
        cert={"key_epoch": 1},
    )
    assert await epochs.get("sp") is None


async def test_the_first_epoch_zero_has_predecessor_zero(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    await epochs.confirm("sp", 0, now=1)
    assert (await epochs.get("sp")).previous == 0


# ── Strict mode (v_50, migration 0015) ──────────────────────────────


async def test_strict_state_defaults_to_trusted_without_keys(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    state = await epochs.get_strict("sp")
    assert state is not None
    assert state.publish_mode == "trusted" and not state.strict
    assert state.writer_pk_for(0) is None
    assert await epochs.get_strict("unknown") is None


async def test_publish_mode_moves_only_forward_in_time(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    assert await epochs.set_publish_mode("sp", "strict", at=100)
    assert (await epochs.get_strict("sp")).strict
    # An older notice (a replay) never moves it back.
    assert not await epochs.set_publish_mode("sp", "trusted", at=99)
    assert (await epochs.get_strict("sp")).strict
    assert await epochs.set_publish_mode("sp", "trusted", at=101)
    state = await epochs.get_strict("sp")
    assert (state.publish_mode, state.mode_at) == ("trusted", 101)


async def test_publish_mode_check_constraint(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    with pytest.raises(sqlite3.IntegrityError):
        await epochs.set_publish_mode("sp", "open", at=1)


async def test_writer_key_pins_keep_current_and_previous(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    assert await epochs.pin_writer_key("sp", 3, "k3", replace=False)
    assert await epochs.pin_writer_key("sp", 4, "k4", replace=False)
    state = await epochs.get_strict("sp")
    assert state.writer_pk_for(4) == "k4"
    assert state.writer_pk_for(3) == "k3"
    assert state.writer_pk_for(2) is None
    assert await epochs.pin_writer_key("sp", 6, "k6", replace=False)
    state = await epochs.get_strict("sp")
    assert (state.writer_key_epoch, state.writer_key_prev_epoch) == (6, 4)
    assert state.writer_pk_for(3) is None


async def test_writer_key_same_epoch_only_with_replace(gfs_db):
    _repo, epochs = await _epochs(gfs_db)
    await epochs.pin_writer_key("sp", 3, "k3", replace=False)
    assert not await epochs.pin_writer_key("sp", 3, "evil", replace=False)
    assert (await epochs.get_strict("sp")).writer_pk_for(3) == "k3"
    # Older never lands.
    assert not await epochs.pin_writer_key("sp", 2, "old", replace=True)
    assert await epochs.pin_writer_key("sp", 3, "owner", replace=True)
    state = await epochs.get_strict("sp")
    assert state.writer_pk_for(3) == "owner"
    # Replacing the same epoch keeps the previous slot untouched.
    assert state.writer_key_prev_epoch is None


async def test_writer_keys_are_forgotten_on_repin_but_mode_stays(gfs_db):
    repo, epochs = await _epochs(gfs_db, identity_public_key="aa" * 32)
    await epochs.pin_writer_key("sp", 3, "k3", replace=False)
    await epochs.set_publish_mode("sp", "strict", at=5)
    assert await repo.set_space_authority(
        "sp",
        expected_pk="aa" * 32,
        expected_cert=None,
        new_pk="bb" * 32,
        cert={"key_epoch": 1},
    )
    state = await epochs.get_strict("sp")
    assert state.writer_pk_for(3) is None
    assert state.strict


async def test_upsert_keeps_strict_state(gfs_db):
    repo, epochs = await _epochs(gfs_db)
    await epochs.pin_writer_key("sp", 3, "k3", replace=False)
    await epochs.set_publish_mode("sp", "strict", at=5)
    await repo.upsert_space(GlobalSpace(space_id="sp", owning_instance="o", name="x"))
    state = await epochs.get_strict("sp")
    assert state.strict and state.writer_pk_for(3) == "k3"


async def test_relay_bytes_sums_only_unexpired_relay_rows(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    kw = {"max_per_recipient": 10, "max_bytes_per_recipient": 10**6}
    await queue.enqueue("a" * 32, "x" * 10, created_at=0, expires_at=100, **kw)
    await queue.enqueue(
        "a" * 32, "y" * 7, created_at=0, expires_at=100, frame_type="relay", **kw
    )
    await queue.enqueue(
        "a" * 32, "z" * 5, created_at=0, expires_at=50, frame_type="relay", **kw
    )
    assert await queue.relay_bytes(60) == 7
    assert await queue.relay_bytes(0) == 12


async def test_recently_seen_subscribers_need_a_held_ws_session(gfs_db):
    repo = SqliteGfsFederationRepo(gfs_db)
    await _owner_row(repo)
    await repo.upsert_space(GlobalSpace(space_id="sp", owning_instance="o"))
    for iid, status in (
        ("seen", "active"),
        ("never", "active"),
        ("old", "active"),
        ("banned", "banned"),
    ):
        await repo.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key="ab" * 32,
                status=status,
            )
        )
        await repo.add_subscriber(space_id="sp", instance_id=iid)
    now = int(time.time())
    for iid in ("seen", "banned"):
        await repo.mark_relay_seen(iid, at=now)
    await repo.mark_relay_seen("old", at=now - 2 * 86400)
    # A bare hello (rtc_connections) earns nothing.
    await repo.upsert_rtc_connection("never", transport="websocket")
    assert await repo.list_recently_seen_subscribers("sp", within_s=86400) == {"seen"}


async def test_recently_seen_follower_households_need_a_held_ws_session(gfs_db):
    """Only active follower households seen within the window get a public
    moment queued; a household with two following users counts once."""
    fed = SqliteGfsFederationRepo(gfs_db)
    users = SqliteGfsUserRegistrationRepo(gfs_db)
    follows = SqliteGfsMomentFollowRepo(gfs_db)
    for iid, status in (
        ("author", "active"),
        ("seen", "active"),
        ("never", "active"),
        ("old", "active"),
        ("banned", "banned"),
        ("other", "active"),
    ):
        await fed.upsert_instance(
            ClientInstance(
                instance_id=iid, display_name=iid, public_key="ab" * 32, status=status
            )
        )
    for user in ("u-a", "u-b"):
        await users.upsert(
            GfsUserRegistration(
                user_id=user,
                instance_id="author",
                username=user,
                display_name=user,
                picture_url=None,
                home_instance_pk="aa" * 32,
                registered_at=0,
            )
        )
    for fan, iid, followed in (
        ("f1", "seen", "u-a"),
        ("f2", "seen", "u-a"),
        ("f3", "never", "u-a"),
        ("f4", "old", "u-a"),
        ("f5", "banned", "u-a"),
        ("f6", "other", "u-b"),
    ):
        await follows.upsert(
            GfsMomentFollow(
                follower_user_id=fan,
                follower_instance_id=iid,
                followed_user_id=followed,
                created_at=0,
            )
        )
    now = int(time.time())
    for iid in ("seen", "banned", "other"):
        await fed.mark_relay_seen(iid, at=now)
    await fed.mark_relay_seen("old", at=now - 2 * 86400)
    assert await follows.recently_seen_households("u-a", within_s=86400) == {"seen"}


async def test_a_kept_key_can_be_handed_back_undelivered(gfs_db):
    """``mark_undelivered`` flips only the generation it names — a newer key
    that replaced it meanwhile is left as it is."""
    fed = SqliteGfsFederationRepo(gfs_db)
    for iid in ("author", "fan"):
        await fed.upsert_instance(
            ClientInstance(
                instance_id=iid, display_name=iid, public_key="ab" * 32, status="active"
            )
        )
    await SqliteGfsUserRegistrationRepo(gfs_db).upsert(
        GfsUserRegistration(
            user_id="u-a",
            instance_id="author",
            username="a",
            display_name="a",
            picture_url=None,
            home_instance_pk="aa" * 32,
            registered_at=0,
        )
    )
    mailbox = SqliteGfsMomentKeyMailboxRepo(gfs_db)
    mail = GfsMomentKeyMail(
        author_user_id="u-a",
        follower_instance_id="fan",
        key_gen=7,
        key_step=0,
        delivery="{}",
        delivered=True,
    )
    await mailbox.put(mail)
    await mailbox.mark_undelivered("u-a", "fan", 6)
    got = await mailbox.get("u-a", "fan")
    assert got is not None and got.delivered
    await mailbox.mark_undelivered("u-a", "fan", 7)
    assert [m.key_gen for m in await mailbox.undelivered_for("fan", limit=5)] == [7]
    # Never back: an older generation, or an older step of the same one, is
    # not kept (one conditional upsert); the same or a newer key is.
    assert not await mailbox.put(replace(mail, key_gen=6, key_step=5))
    assert await mailbox.put(replace(mail, key_step=3, delivery='{"s":3}'))
    assert not await mailbox.put(replace(mail, key_step=2))
    assert await mailbox.put(replace(mail, key_step=3, delivery='{"s":3}'))
    got = await mailbox.get("u-a", "fan")
    assert got is not None and (got.key_gen, got.key_step) == (7, 3)
    assert got.delivery == '{"s":3}'


async def _relay(queue, to, body, *, now=0, rows=10, per=10**6, total=10**6):
    return await queue.enqueue_relay(
        to,
        body,
        created_at=now,
        expires_at=now + 100,
        max_per_recipient=rows,
        max_bytes_per_recipient=per,
        max_total_bytes=total,
    )


async def _relay_rows(queue, to):
    return [r for r in await queue.list_for(to, now=0) if r.frame_type == "relay"]


async def test_enqueue_relay_evicts_the_recipients_own_oldest_at_its_caps(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    for i in range(3):
        assert await _relay(queue, "a" * 32, f'{{"n":{i}}}', rows=2)
    assert [r.sealed["n"] for r in await _relay_rows(queue, "a" * 32)] == [1, 2]


async def test_enqueue_relay_makes_room_from_the_largest_holder(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    blob = '{"x":"' + "h" * 90 + '"}'
    for _ in range(3):
        assert await _relay(queue, "h" * 32, blob, total=300)
    assert await _relay(queue, "l" * 32, blob, total=300)
    assert len(await _relay_rows(queue, "l" * 32)) == 1
    assert len(await _relay_rows(queue, "h" * 32)) == 2
    assert await queue.relay_bytes(0) <= 300


async def test_enqueue_relay_never_touches_envelope_rows(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    await queue.enqueue(
        "a" * 32,
        '{"e":"' + "e" * 44 + '"}',
        created_at=0,
        expires_at=100,
        max_per_recipient=10,
        max_bytes_per_recipient=10**6,
    )
    assert await _relay(queue, "a" * 32, '{"r":"' + "r" * 44 + '"}', total=60)
    rows = await queue.list_for("a" * 32, now=0)
    assert sorted(r.frame_type for r in rows) == ["envelope", "relay"]


async def test_enqueue_relay_refuses_an_item_bigger_than_a_cap(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    assert not await _relay(queue, "a" * 32, "r" * 50, per=10)
    assert not await _relay(queue, "a" * 32, "r" * 50, total=10)
    assert not await _relay(queue, "a" * 32, "r" * 5, rows=0)


_KIND = '{"type":"moment_reaction"'


async def _kind(queue, to, n, *, now=0, rows=10, per=10**6, total=10**6, cap=2):
    return await queue.enqueue_relay(
        to,
        f'{_KIND},"n":{n}}}',
        created_at=now,
        expires_at=now + 100,
        max_per_recipient=rows,
        max_bytes_per_recipient=per,
        max_total_bytes=total,
        kind_prefix=_KIND,
        max_kind_per_recipient=cap,
        max_kind_row_bytes=200,
    )


async def test_enqueue_relay_a_capped_kind_evicts_only_its_own_kind(gfs_db):
    # #1179 F1: a capped kind (sealed reactions) is held to its own row cap
    # and makes room only from its own kind — never a moment or space item.
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    assert await _relay(queue, "a" * 32, '{"item":0}', rows=4)
    for i in range(5):
        assert await _kind(queue, "a" * 32, i, rows=4)
    assert await _relay(queue, "a" * 32, '{"item":1}', rows=4)
    rows = [r.sealed for r in await _relay_rows(queue, "a" * 32)]
    assert {"item": 0} in rows and {"item": 1} in rows
    assert [r["n"] for r in rows if "n" in r] == [3, 4]


async def test_enqueue_relay_a_capped_kind_never_evicts_other_kinds(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    for i in range(3):
        assert await _relay(queue, "a" * 32, f'{{"item":{i}}}', rows=3)
    # The recipient is full of other kinds: the capped item is dropped.
    assert not await _kind(queue, "a" * 32, 0, rows=3)
    # Server-wide: it never evicts another recipient's row either.
    blob = '{"x":"' + "h" * 90 + '"}'
    assert await _relay(queue, "b" * 32, blob)
    total = await queue.relay_bytes(0)
    assert not await _kind(queue, "c" * 32, 1, total=total + 10)
    assert [r.sealed for r in await _relay_rows(queue, "a" * 32)] == [
        {"item": 0},
        {"item": 1},
        {"item": 2},
    ]
    assert len(await _relay_rows(queue, "b" * 32)) == 1
    assert await _relay_rows(queue, "c" * 32) == []


async def test_enqueue_relay_refuses_a_capped_kind_row_over_its_size(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    assert not await queue.enqueue_relay(
        "a" * 32,
        _KIND + ',"pad":"' + "p" * 300 + '"}',
        created_at=0,
        expires_at=100,
        max_per_recipient=10,
        max_bytes_per_recipient=10**6,
        max_total_bytes=10**6,
        kind_prefix=_KIND,
        max_kind_per_recipient=2,
        max_kind_row_bytes=200,
    )


_RELAY_SUM_QUERIES = (
    (
        "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM gfs_envelope_queue"
        " WHERE frame_type='relay' AND expires_at > 0 AND to_instance='a'"
    ),
    (
        "SELECT COALESCE(SUM(size_bytes), 0) FROM gfs_envelope_queue"
        " WHERE frame_type='relay' AND expires_at > 0"
    ),
    (
        "SELECT to_instance FROM gfs_envelope_queue"
        " WHERE frame_type='relay' AND expires_at > 0 GROUP BY to_instance"
        " ORDER BY SUM(size_bytes) DESC, to_instance = 'a' ASC LIMIT 1"
    ),
)


@pytest.mark.parametrize("sql", _RELAY_SUM_QUERIES)
async def test_relay_cap_queries_read_only_the_covering_index(gfs_db, sql):
    """The relay caps are summed inside the writer transaction on every
    insert — they must never read the stored blobs (round-3 review)."""
    rows = await gfs_db.fetchall("EXPLAIN QUERY PLAN " + sql)
    plan = " ".join(str(dict(r).get("detail", "")) for r in rows)
    assert "USING COVERING INDEX idx_gfs_envelope_queue_relay_size" in plan, plan


async def test_relay_insert_cost_stays_flat_at_a_full_cap(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    blob = "x" * (64 * 1024)
    cap = 40 * 4 * len(blob)

    async def _put(to: str) -> float:
        started = time.perf_counter()
        assert await _relay(queue, to, blob, rows=250, per=4 * len(blob), total=cap)
        return time.perf_counter() - started

    early = [await _put(f"r{i % 40}") for i in range(20)]
    for i in range(20, 160):
        await _put(f"r{i % 40}")
    at_cap = [await _put(f"new{i}") for i in range(20)]
    assert await queue.relay_bytes(0) <= cap
    # Flat: the full-cap insert (eviction included) costs about what an
    # insert into a near-empty queue does, never orders of magnitude more.
    assert sorted(at_cap)[10] < max(5 * sorted(early)[10], 0.02)


async def test_size_bytes_is_written_for_every_row(gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    await _relay(queue, "a" * 32, '{"r":1}')
    await queue.enqueue(
        "a" * 32,
        '{"e":2}',
        created_at=0,
        expires_at=100,
        max_per_recipient=10,
        max_bytes_per_recipient=10**6,
    )
    rows = await gfs_db.fetchall(
        "SELECT size_bytes, LENGTH(sealed_json) AS n FROM gfs_envelope_queue"
    )
    assert all(r["size_bytes"] == r["n"] for r in rows)


# ── Opaque channels (v_51) ───────────────────────────────────────────────


async def test_channel_repo_pin_epoch_writer_key_and_idle_sweep(gfs_db):
    repo = SqliteGfsChannelRepo(gfs_db)
    cid = "0" * 31 + "a"
    assert await repo.register(cid, channel_suite="ed25519", channel_pk="pk", now=100)
    assert not await repo.register(
        cid, channel_suite="ed25519", channel_pk="x", now=100
    )
    assert (await repo.get(cid)).channel_pk == "pk"
    # CAS epoch steps.
    assert await repo.set_epoch(cid, 5, expected=None, now=110)
    assert not await repo.set_epoch(cid, 7, expected=None, now=111)
    assert not await repo.set_epoch(cid, 4, expected=5, now=112)
    assert await repo.set_epoch(cid, 6, expected=5, now=113)
    row = await repo.get(cid)
    assert (row.epoch, row.epoch_prev, row.epoch_raised_at) == (6, 5, 113)
    # First pin per epoch wins; a newer epoch moves the old one to prev.
    assert await repo.pin_writer_key(cid, 6, "w6")
    assert not await repo.pin_writer_key(cid, 6, "evil")
    assert await repo.pin_writer_key(cid, 7, "w7")
    row = await repo.get(cid)
    assert (row.writer_pk_for(6), row.writer_pk_for(7)) == ("w6", "w7")
    # Count, and the unused-row sweep (no notice, no seat) spares used rows.
    assert await repo.count() == 1
    unused = "1" * 32
    assert await repo.register(unused, channel_suite="ed25519", channel_pk="u", now=50)
    assert await repo.prune_unused(older_than=60) == 1
    assert await repo.get(unused) is None
    assert await repo.get(cid) is not None
    # The id column only takes a channel id shape (CHECK; OR IGNORE drops it).
    assert not await repo.register(
        "space-id-like", channel_suite="ed25519", channel_pk="p", now=1
    )
    assert await repo.get("space-id-like") is None
    assert await repo.prune_idle(older_than=200) == 1
    assert await repo.get(cid) is None


# ── Cluster node approval ─────────────────────────────────────────────


@pytest.mark.security
async def test_cluster_approve_never_moves_an_approved_key(gfs_db):
    """An approved cluster key is immutable in SQL too: a second approval
    under another key keeps the first. Rotation is delete then re-add."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.approve_node("b", "http://b", "aa" * 32)
    await repo.approve_node("b", "http://b2", "bb" * 32)
    (row,) = await repo.list_nodes()
    assert (row.approved_key, row.public_key) == ("aa" * 32, "aa" * 32)
    # Nor does the losing approval move the URL the key was approved with.
    assert (row.url, row.approved_url) == ("http://b", "http://b")
    # Re-approving the SAME key refreshes the URL.
    await repo.approve_node("b", "http://b3", "aa" * 32)
    (row,) = await repo.list_nodes()
    assert (row.url, row.approved_url) == ("http://b3", "http://b3")


async def test_cluster_approve_fills_a_row_without_approval(gfs_db):
    """A shared-seed or pre-upgrade row (no approval, any legacy
    ``public_key``) takes the admin's key — in both columns, so an
    old-version node sharing the DB honours it too."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://b", public_key="ee" * 32)
    )
    await repo.approve_node("b", "http://b", "cc" * 32)
    (row,) = await repo.list_nodes()
    assert (row.approved_key, row.public_key) == ("cc" * 32, "cc" * 32)


async def test_cluster_insert_never_approves(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://b", public_key="aa" * 32)
    )
    assert (await repo.list_nodes())[0].approved_key == ""


async def test_cluster_remove_then_re_approve_rotates_the_key(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    await repo.approve_node("b", "http://b", "aa" * 32)
    await repo.remove_node("b")
    await repo.approve_node("b", "http://b", "dd" * 32)
    (row,) = await repo.list_nodes()
    assert row.approved_key == "dd" * 32


async def test_cluster_touch_node_updates_only_an_existing_row(gfs_db):
    """A liveness refresh never creates a row and never touches a key."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.touch_node("ghost", status="online", last_seen="2026-01-01 00:00:00")
    assert await repo.list_nodes() == []
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://b", public_key="aa" * 32)
    )
    await repo.touch_node(
        "b", status="online", last_seen="2026-01-02 00:00:00", sibling_url="http://x"
    )
    (row,) = await repo.list_nodes()
    assert (row.url, row.public_key, row.status, row.last_seen) == (
        "http://x",
        "aa" * 32,
        "online",
        "2026-01-02 00:00:00",
    )


async def test_cluster_touch_node_sibling_url_replaces_an_unapproved_url(gfs_db):
    """A shared-seed sibling redeployed on a new port: its URL follows."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://b.test:1111", public_key="aa" * 32)
    )
    await repo.touch_node(
        "b", status="online", last_seen=None, sibling_url="http://b.test:2222"
    )
    assert (await repo.list_nodes())[0].url == "http://b.test:2222"


@pytest.mark.security
async def test_cluster_touch_node_sibling_url_never_moves_an_approved_row(gfs_db):
    """An approved node's URL never moves in-band — enforced in the UPDATE,
    so an approval racing the caller's read still wins."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.approve_node("b", "http://b", "aa" * 32)
    await repo.touch_node(
        "b", status="online", last_seen=None, sibling_url="http://evil"
    )
    (row,) = await repo.list_nodes()
    assert (row.url, row.approved_url, row.status) == (
        "http://b",
        "http://b",
        "online",
    )


async def test_cluster_touch_node_empty_sibling_url_keeps_the_url(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(ClusterNode(node_id="b", url="http://b.test"))
    await repo.touch_node("b", status="online", last_seen=None)
    await repo.touch_node("b", status="online", last_seen=None, sibling_url="")
    assert (await repo.list_nodes())[0].url == "http://b.test"


async def test_cluster_insert_node_never_overwrites_a_row(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://b", public_key="aa" * 32)
    )
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://evil", public_key="bb" * 32)
    )
    (row,) = await repo.list_nodes()
    assert (row.url, row.public_key) == ("http://b", "aa" * 32)


async def test_cluster_reclaim_node_rekeys_only_an_unapproved_row(gfs_db):
    """A shared-seed sibling reclaims a row an old-version node rewrote;
    an approved row — and a missing one — is left alone."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(node_id="b", url="http://evil", public_key="ee" * 32)
    )
    await repo.approve_node("c", "http://c", "cc" * 32)
    for node_id in ("b", "c", "ghost"):
        await repo.reclaim_node(
            node_id,
            url="http://b",
            public_key="aa" * 32,
            status="online",
            last_seen="2026-01-02 00:00:00",
        )
    rows = {r.node_id: r for r in await repo.list_nodes()}
    assert set(rows) == {"b", "c"}
    assert (rows["b"].url, rows["b"].public_key, rows["b"].status) == (
        "http://b",
        "aa" * 32,
        "online",
    )
    assert (rows["c"].url, rows["c"].public_key) == ("http://c", "cc" * 32)


async def test_cluster_approve_writes_the_approved_url_alone(gfs_db):
    """``approved_url`` is the operator's: approval writes it, and nothing a
    HELLO or a liveness refresh does moves it."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.approve_node("b", "http://b", "aa" * 32)
    await repo.touch_node("b", status="online", last_seen=None, sibling_url="http://x")
    await repo.reclaim_node(
        "b", url="http://evil", public_key="ee" * 32, status="online", last_seen=""
    )
    (row,) = await repo.list_nodes()
    assert (row.url, row.approved_url) == ("http://b", "http://b")


async def test_cluster_remove_stale_siblings_drops_only_stale_own_key_rows(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    own, other = "aa" * 32, "bb" * 32
    old, new = "2026-01-01 00:00:00", "2026-06-01 00:00:00"
    rows = [
        ("stale", own, old),
        ("stale-tz-aware", own, "2026-01-01T00:00:00+00:00"),
        ("fresh", own, new),
        ("self", own, old),
        ("foreign", other, old),
        ("garbled", own, "not a date"),
    ]
    for node_id, key, seen in rows:
        await repo.insert_node(
            ClusterNode(node_id=node_id, url="", public_key=key, last_seen=seen)
        )
    await repo.approve_node("approved", "http://a", own)
    await repo.touch_node("approved", status="offline", last_seen=old)
    removed = await repo.remove_stale_siblings(
        own_key=own.upper(), seen_before="2026-03-01 00:00:00", keep_node_id="self"
    )
    assert removed == 2
    assert {r.node_id for r in await repo.list_nodes()} == {
        "fresh",
        "self",
        "foreign",
        "garbled",
        "approved",
    }


async def test_add_subscriber_says_whether_it_created_the_seat(fed):
    """The INSERT itself decides "new seat" (review M2), so two concurrent
    subscribes for one seat can never both notify ``subscribe``."""
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o", display_name="O", public_key="aa" * 32, status="active"
        )
    )
    await fed.upsert_space(
        GlobalSpace(space_id="sp-new", owning_instance="o", status="active")
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="s", display_name="S", public_key="ab" * 32, status="active"
        )
    )
    assert await fed.add_subscriber(space_id="sp-new", instance_id="s") is True
    assert await fed.add_subscriber(space_id="sp-new", instance_id="s") is False
    assert [s.instance_id for s in await fed.list_subscribers("sp-new")] == ["s"]


# ── Space media cache (GFS 0025) ──────────────────────────────────────


def _media_blob(
    n: int,
    *,
    size=100,
    created=1_000,
    expires=5_000,
    fetched=None,
    space="s1",
    kind="space",
    household="h1",
    owner_kind="household",
):
    return GfsSpaceMedia(
        blob_id=f"{n:064x}",
        size=size,
        created_at=created,
        expires_at=expires,
        last_fetched_at=created if fetched is None else fetched,
        scope_kind=kind,
        scope_id=space,
        owner_kind=owner_kind,
        owner_id=household,
    )


@pytest.fixture
async def space_media(gfs_db):
    return SqliteGfsSpaceMediaRepo(gfs_db)


async def test_space_media_add_blob_is_insert_or_ignore(space_media):
    assert await space_media.add_blob(_media_blob(1)) is True
    assert await space_media.add_blob(_media_blob(1, space="other")) is False
    got = await space_media.get_blob(f"{1:064x}")
    assert (got.scope_kind, got.scope_id, got.size) == ("space", "s1", 100)
    assert await space_media.get_blob(f"{2:064x}") is None


async def test_space_media_totals_and_scope_bytes_are_trigger_kept(space_media):
    await space_media.add_blob(_media_blob(1, size=100))
    await space_media.add_blob(_media_blob(2, size=300))
    await space_media.add_blob(_media_blob(3, size=50, space="c1", kind="channel"))
    assert await space_media.totals() == (3, 450)
    assert await space_media.scope_bytes("space", "s1") == 400
    assert await space_media.scope_bytes("channel", "c1") == 50
    assert await space_media.largest_scopes(limit=5) == [
        ("space", "s1", 400),
        ("channel", "c1", 50),
    ]
    assert await space_media.delete_blobs([f"{1:064x}", f"{9:064x}"]) == [f"{1:064x}"]
    assert await space_media.totals() == (2, 350)
    assert await space_media.scope_bytes("space", "s1") == 300


async def test_space_media_servable_unless_expired_or_banned(space_media, fed):
    await fed.upsert_instance(
        ClientInstance(instance_id="o", display_name="O", public_key="aa" * 32)
    )
    await fed.upsert_space(
        GlobalSpace(space_id="ok", owning_instance="o", status="active")
    )
    await fed.upsert_space(
        GlobalSpace(space_id="bad", owning_instance="o", status="banned")
    )
    await space_media.add_blob(_media_blob(1, space="ok"))
    await space_media.add_blob(_media_blob(2, space="bad"))
    await space_media.add_blob(_media_blob(3, space="ok", expires=1_500))
    await space_media.add_blob(_media_blob(4, space="gone"))
    # A channel whose id happens to equal a banned space id is not that space.
    await space_media.add_blob(_media_blob(5, space="bad", kind="channel"))
    assert await space_media.get_servable(f"{1:064x}", now=2_000) is not None
    assert await space_media.get_servable(f"{2:064x}", now=2_000) is None
    assert await space_media.get_servable(f"{3:064x}", now=2_000) is None
    assert await space_media.get_servable(f"{4:064x}", now=2_000) is not None
    assert await space_media.get_servable(f"{5:064x}", now=2_000) is not None


async def test_space_media_a_delete_takes_only_the_scopes_blob(space_media):
    await space_media.add_blob(_media_blob(1, space="s1"))
    bid = f"{1:064x}"
    assert (
        await space_media.delete_scope_blob(bid, scope_kind="space", scope_id="s2")
        is False
    )
    assert (
        await space_media.delete_scope_blob(bid, scope_kind="channel", scope_id="s1")
        is False
    )
    assert (
        await space_media.delete_scope_blob(bid, scope_kind="space", scope_id="s1")
        is True
    )
    assert await space_media.get_blob(bid) is None


async def test_space_media_owner_bytes_and_lru(space_media):
    """Fairness reads per-owner bytes across scopes — a household's spaces
    (third review A3), a client bucket's channel blobs (fourth review G1)."""
    await space_media.add_blob(_media_blob(1, space="a", household="h1", fetched=300))
    await space_media.add_blob(_media_blob(2, space="b", household="h1", fetched=100))
    await space_media.add_blob(
        _media_blob(3, space="c", household="k9", owner_kind="client", size=150)
    )
    assert await space_media.owner_bytes("household", "h1") == 200
    assert await space_media.owner_bytes("client", "h1") == 0
    assert await space_media.largest_owners(limit=5) == [
        ("household", "h1", 200),
        ("client", "k9", 150),
    ]
    assert [
        m.blob_id for m in await space_media.owner_lru("household", "h1", limit=5)
    ] == [f"{2:064x}", f"{1:064x}"]
    got = await space_media.get_blob(f"{3:064x}")
    assert (got.owner_kind, got.owner_id) == ("client", "k9")


async def test_space_media_scope_lru_and_blob_ids(space_media):
    await space_media.add_blob(_media_blob(1, fetched=300))
    await space_media.add_blob(_media_blob(2, fetched=100))
    await space_media.add_blob(_media_blob(3, fetched=200, space="s2"))
    assert [m.blob_id for m in await space_media.scope_lru("space", "s1", limit=5)] == [
        f"{2:064x}",
        f"{1:064x}",
    ]
    assert sorted(await space_media.scope_blob_ids("space", "s1")) == [
        f"{1:064x}",
        f"{2:064x}",
    ]


async def test_space_media_touch_is_throttled(space_media):
    await space_media.add_blob(_media_blob(1, created=1_000))
    assert await space_media.touch(f"{1:064x}", now=1_500, min_interval=3600) is False
    assert await space_media.touch(f"{1:064x}", now=4_700, min_interval=3600) is True
    assert (await space_media.get_blob(f"{1:064x}")).last_fetched_at == 4_700


async def test_space_media_expired_and_known(space_media):
    await space_media.add_blob(_media_blob(1, expires=150))
    await space_media.add_blob(_media_blob(2, expires=100))
    await space_media.add_blob(_media_blob(3, expires=900))
    assert [m.blob_id for m in await space_media.expired(200, limit=10)] == [
        f"{2:064x}",
        f"{1:064x}",
    ]
    assert await space_media.known_blob_ids([]) == set()
    assert await space_media.known_blob_ids([f"{3:064x}", f"{9:064x}"]) == {f"{3:064x}"}
    assert await space_media.delete_blobs([]) == []


async def test_space_media_upload_counts(space_media):
    for _ in range(3):
        await space_media.reserve_upload("space", "s1", bucket=10, cap=99)
    await space_media.reserve_upload("space", "s1", bucket=9, cap=99)
    await space_media.reserve_upload("space", "s1", bucket=5, cap=99)
    await space_media.reserve_upload("channel", "s1", bucket=10, cap=99)
    assert await space_media.upload_counts("space", "s1", bucket=10) == (3, 1)
    assert await space_media.upload_counts("channel", "s1", bucket=10) == (1, 0)
    assert await space_media.prune_upload_counts(before_bucket=9) == 1
    assert await space_media.upload_counts("space", "s1", bucket=6) == (0, 0)


async def test_space_media_reserve_upload_is_atomic_and_refundable(space_media):
    """Upsert-then-check in one statement: a slot is taken only while the
    count is under the cap, and a failed upload gives it back."""
    assert await space_media.reserve_upload("space", "s1", bucket=10, cap=2) is True
    assert await space_media.reserve_upload("space", "s1", bucket=10, cap=2) is True
    assert await space_media.reserve_upload("space", "s1", bucket=10, cap=2) is False
    assert await space_media.reserve_upload("space", "s1", bucket=10, cap=0) is False
    assert await space_media.upload_counts("space", "s1", bucket=10) == (2, 0)
    await space_media.refund_upload("space", "s1", bucket=10)
    assert await space_media.upload_counts("space", "s1", bucket=10) == (1, 0)
    await space_media.refund_upload("space", "s1", bucket=10)
    await space_media.refund_upload("space", "s1", bucket=10)
    assert await space_media.upload_counts("space", "s1", bucket=10) == (0, 0)


async def test_space_media_ip_bytes_are_atomic_and_refundable(space_media):
    assert await space_media.reserve_ip_bytes("k", bucket=10, size=60, cap=100)
    assert not await space_media.reserve_ip_bytes("k", bucket=10, size=60, cap=100)
    assert not await space_media.reserve_ip_bytes("k", bucket=10, size=200, cap=100)
    assert await space_media.reserve_ip_bytes("k", bucket=9, size=5, cap=100)
    assert await space_media.ip_bytes("k", bucket=10) == (60, 5)
    await space_media.refund_ip_bytes("k", bucket=10, size=100)
    assert await space_media.ip_bytes("k", bucket=10) == (0, 5)
    assert await space_media.prune_ip_bytes(before_bucket=10) == 1


async def test_space_media_sweep_lease_and_cursor(space_media):
    assert await space_media.acquire_sweep_lease("a", now=100, ttl=50) is True
    assert await space_media.acquire_sweep_lease("b", now=120, ttl=50) is False
    # The holder renews; another node takes over once the lease ran out.
    assert await space_media.acquire_sweep_lease("a", now=130, ttl=50) is True
    assert await space_media.acquire_sweep_lease("b", now=181, ttl=50) is True
    assert await space_media.sweep_cursor() == ""
    await space_media.set_sweep_cursor("abc")
    assert await space_media.sweep_cursor() == "abc"


# ─── Space media demand (GFS 0026, v_83) ───────────────────────────────


async def test_demand_counts_requests_in_a_window(gfs_db):
    repo = SqliteGfsSpaceMediaDemandRepo(gfs_db)
    b = "a" * 64
    got = await repo.note_request(b, "space", "s1", now=1000, window_s=60)
    assert got == GfsSpaceMediaDemand(
        blob_id=b, scope_kind="space", scope_id="s1", stored_at=None, requests=1
    )
    got = await repo.note_request(b, "space", "s1", now=1030, window_s=60)
    assert got.requests == 2
    # Past the window: a new one starts.
    got = await repo.note_request(b, "space", "s1", now=1061, window_s=60)
    assert got.requests == 1
    # Another scope naming the same id is its own row.
    other = await repo.note_request(b, "channel", "c1", now=1061, window_s=60)
    assert other.requests == 1


async def test_demand_remembers_when_a_blob_was_stored(gfs_db):
    repo = SqliteGfsSpaceMediaDemandRepo(gfs_db)
    b = "b" * 64
    await repo.note_dropped(
        [
            GfsSpaceMedia(
                blob_id=b,
                size=10,
                created_at=500,
                expires_at=600,
                last_fetched_at=500,
                scope_kind="space",
                scope_id="s1",
            )
        ],
        now=700,
    )
    got = await repo.note_request(b, "space", "s1", now=800, window_s=60)
    assert (got.stored_at, got.requests) == (500, 1)
    # A request first, a drop later (a re-upload that left again) keeps the
    # count and learns the age.
    c = "c" * 64
    await repo.note_request(c, "space", "s1", now=800, window_s=600)
    await repo.note_dropped(
        [
            GfsSpaceMedia(
                blob_id=c,
                size=10,
                created_at=750,
                expires_at=790,
                last_fetched_at=750,
                scope_kind="space",
                scope_id="s1",
            )
        ],
        now=810,
    )
    got = await repo.note_request(c, "space", "s1", now=820, window_s=600)
    assert (got.stored_at, got.requests) == (750, 2)


async def test_demand_claim_is_one_per_window(gfs_db):
    repo = SqliteGfsSpaceMediaDemandRepo(gfs_db)
    b = "d" * 64
    await repo.note_request(b, "space", "s1", now=100, window_s=60)
    assert await repo.claim(b, "space", "s1", now=100, hold_s=60)
    assert not await repo.claim(b, "space", "s1", now=150, hold_s=60)
    assert await repo.claim(b, "space", "s1", now=160, hold_s=60)
    # No row, no claim (a claim follows a noted request).
    assert not await repo.claim("e" * 64, "space", "s1", now=160, hold_s=60)


async def test_demand_prune(gfs_db):
    repo = SqliteGfsSpaceMediaDemandRepo(gfs_db)
    await repo.note_request("f" * 64, "space", "s1", now=100, window_s=60)
    await repo.note_request("0" * 64, "space", "s1", now=900, window_s=60)
    assert await repo.prune(before=500) == 1
    assert (
        await repo.note_request("0" * 64, "space", "s1", now=901, window_s=60)
    ).requests == 2


async def test_demand_claim_can_be_given_back(gfs_db):
    repo = SqliteGfsSpaceMediaDemandRepo(gfs_db)
    b = "9" * 64
    await repo.note_request(b, "space", "s1", now=100, window_s=60)
    assert await repo.claim(b, "space", "s1", now=100, hold_s=60)
    # Another claim's value never releases it.
    await repo.release_claim(b, "space", "s1", until=999)
    assert not await repo.claim(b, "space", "s1", now=101, hold_s=60)
    await repo.release_claim(b, "space", "s1", until=160)
    assert await repo.claim(b, "space", "s1", now=101, hold_s=60)


# ─── Space media member wants (GFS 0027) ───────────────────────────────


def _mwant(ticket: str, *, scope_id="s1", created=100, expires=200):
    return GfsSpaceMediaMemberWant(
        ticket=ticket,
        scope_kind="space",
        scope_id=scope_id,
        blob_id="a" * 64,
        mode="stream",
        hints="[]",
        expires_at=expires,
        created_at=created,
    )


async def test_member_wants_claim_the_oldest_once(gfs_db):
    repo = SqliteGfsSpaceMediaMemberWantRepo(gfs_db)
    await repo.post(_mwant("t2", created=101))
    await repo.post(_mwant("t1", created=100))
    await repo.post(_mwant("tx", scope_id="s2"))
    got = await repo.claim_next(
        "space", "s1", now=150, hold_s=10, max_attempts=3, claim_hash="h"
    )
    assert got is not None and got.ticket == "t1" and got.claimed_until == 160
    assert await repo.claimed("t1", now=150, claim_hash="h")
    # Only the claim's own secret hash counts.
    assert not await repo.claimed("t1", now=150, claim_hash="other")
    assert not await repo.claimed("t1", now=150, claim_hash="")
    assert got.claim_hash == "h"
    got = await repo.claim_next(
        "space", "s1", now=150, hold_s=10, max_attempts=3, claim_hash="h"
    )
    assert got is not None and got.ticket == "t2"
    # Every want of s1 is claimed: nothing more for s1 (s2 is its own).
    assert (
        await repo.claim_next(
            "space", "s1", now=151, hold_s=10, max_attempts=3, claim_hash="h"
        )
    ) is None


async def test_member_wants_concurrent_pulls_claim_once(gfs_db):
    repo = SqliteGfsSpaceMediaMemberWantRepo(gfs_db)
    await repo.post(_mwant("t1"))
    got = await asyncio.gather(
        *[
            repo.claim_next(
                "space", "s1", now=150, hold_s=10, max_attempts=3, claim_hash="h"
            )
            for _ in range(8)
        ]
    )
    assert [g.ticket for g in got if g is not None] == ["t1"]


async def test_member_wants_an_expired_claim_is_offered_again(gfs_db):
    repo = SqliteGfsSpaceMediaMemberWantRepo(gfs_db)
    await repo.post(_mwant("t1"))
    assert await repo.claim_next(
        "space", "s1", now=150, hold_s=10, max_attempts=3, claim_hash="h"
    )
    assert not await repo.claimed("t1", now=160, claim_hash="h")
    again = await repo.claim_next(
        "space", "s1", now=160, hold_s=10, max_attempts=3, claim_hash="h"
    )
    assert again is not None and again.ticket == "t1"


async def test_member_wants_release_counts_an_attempt(gfs_db):
    repo = SqliteGfsSpaceMediaMemberWantRepo(gfs_db)
    await repo.post(_mwant("t1"))
    for n in range(2):
        got = await repo.claim_next(
            "space", "s1", now=150, hold_s=10, max_attempts=2, claim_hash="h"
        )
        assert got is not None and got.attempts == n
        await repo.release("t1")
        assert not await repo.claimed("t1", now=150, claim_hash="h")
    # Two declines: never offered again.
    assert (
        await repo.claim_next(
            "space", "s1", now=150, hold_s=10, max_attempts=2, claim_hash="h"
        )
    ) is None


async def test_member_wants_expire_open_scopes_and_prune(gfs_db):
    repo = SqliteGfsSpaceMediaMemberWantRepo(gfs_db)
    await repo.post(_mwant("t1", expires=200))
    await repo.post(_mwant("t2", scope_id="s2", expires=300))
    assert await repo.open_scopes(now=150) == {("space", "s1"), ("space", "s2")}
    assert await repo.open_scopes(now=250) == {("space", "s2")}
    # A claimed want is not open until its claim runs out.
    await repo.claim_next(
        "space", "s2", now=250, hold_s=10, max_attempts=3, claim_hash="h"
    )
    assert await repo.open_scopes(now=255) == set()
    assert await repo.open_scopes(now=260) == {("space", "s2")}
    # Past its session: never offered, then pruned.
    assert (
        await repo.claim_next(
            "space", "s1", now=250, hold_s=10, max_attempts=3, claim_hash="h"
        )
    ) is None
    assert await repo.prune(now=250) == 1
    await repo.remove("t2")
    assert await repo.open_scopes(now=0) == set()
    assert not await repo.claimed("t2", now=0, claim_hash="h")
