"""Extra coverage for GFS repositories (admin + federation helpers)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from socialhome.global_server.domain import (
    ClientInstance,
    ClusterNode,
    GfsAppeal,
    GfsFraudReport,
    GfsSubscriberWithKeys,
    GlobalSpace,
)
from socialhome.global_server.repositories import (
    SqliteClusterRepo,
    SqliteGfsAdminRepo,
    SqliteGfsChannelRepo,
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
    SqliteGfsInviteRepo,
    SqliteGfsSpaceEpochRepo,
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
            inbox_url="http://owner",
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
            inbox_url="http://o",
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
    with pytest.raises(Exception):
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
    kw = dict(max_per_recipient=10, max_bytes_per_recipient=10**6)
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
                inbox_url="http://x",
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


_RELAY_SUM_QUERIES = (
    "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM gfs_envelope_queue"
    " WHERE frame_type='relay' AND expires_at > 0 AND to_instance='a'",
    "SELECT COALESCE(SUM(size_bytes), 0) FROM gfs_envelope_queue"
    " WHERE frame_type='relay' AND expires_at > 0",
    "SELECT to_instance FROM gfs_envelope_queue"
    " WHERE frame_type='relay' AND expires_at > 0 GROUP BY to_instance"
    " ORDER BY SUM(size_bytes) DESC, to_instance = 'a' ASC LIMIT 1",
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
    # The URL still refreshes.
    assert row.url == "http://b2"


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
        "b", status="online", last_seen="2026-01-02 00:00:00", url_if_empty="http://x"
    )
    (row,) = await repo.list_nodes()
    assert (row.url, row.public_key, row.status, row.last_seen) == (
        "http://b",
        "aa" * 32,
        "online",
        "2026-01-02 00:00:00",
    )


async def test_cluster_touch_node_fills_only_an_empty_url(gfs_db):
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(ClusterNode(node_id="b", url=""))
    await repo.touch_node(
        "b", status="online", last_seen=None, url_if_empty="http://b.test"
    )
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
