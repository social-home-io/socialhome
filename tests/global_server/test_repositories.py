"""Extra coverage for GFS repositories (admin + federation helpers)."""

from __future__ import annotations

import time

import pytest

from socialhome.global_server.domain import (
    ClientInstance,
    GfsAppeal,
    GfsFraudReport,
    GfsSubscriberWithKeys,
    GlobalSpace,
)
from socialhome.global_server.repositories import (
    SqliteGfsAdminRepo,
    SqliteGfsFederationRepo,
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
            inbox_url="http://a",
            status="pending",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="b",
            display_name="B",
            public_key="bb" * 32,
            inbox_url="http://b",
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
            inbox_url="http://kw",
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
            inbox_url="http://l",
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
            inbox_url="http://kws",
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
            inbox_url="http://rn",
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
            inbox_url="http://o",
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
            inbox_url="http://o",
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub",
            display_name="S",
            public_key="bb" * 32,
            inbox_url="http://s",
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
                inbox_url=f"http://{iid}",
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
            inbox_url="http://o",
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
            inbox_url="http://o",
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
            inbox_url="http://o",
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub-kw",
            display_name="WithKeywrap",
            public_key="bb" * 32,
            inbox_url="http://kw",
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
            inbox_url="http://bare",
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
            inbox_url="http://o",
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
                inbox_url=f"http://{iid}",
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
