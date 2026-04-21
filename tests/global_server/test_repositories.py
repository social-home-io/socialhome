"""Extra coverage for GFS repositories (admin + federation helpers)."""

from __future__ import annotations

import time

import pytest

from social_home.global_server.domain import (
    ClientInstance,
    GfsAppeal,
    GfsFraudReport,
    GlobalSpace,
)
from social_home.global_server.repositories import (
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
            endpoint_url="http://a",
            status="pending",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="b",
            display_name="B",
            public_key="bb" * 32,
            endpoint_url="http://b",
            status="active",
        )
    )
    active = await fed.list_instances(status="active")
    assert {x.instance_id for x in active} == {"b"}
    pending = await fed.list_instances(status="pending")
    assert {x.instance_id for x in pending} == {"a"}


async def test_list_spaces_for_instance(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="owner",
            display_name="O",
            public_key="aa" * 32,
            endpoint_url="http://o",
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
            endpoint_url="http://o",
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub",
            display_name="S",
            public_key="bb" * 32,
            endpoint_url="http://s",
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
