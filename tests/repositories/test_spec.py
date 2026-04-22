"""Tests for the :class:`Spec` query builder + the two repos that use it."""

from __future__ import annotations

import pytest

from socialhome.repositories._spec import Spec, spec_to_sql


# ─── spec_to_sql purity ──────────────────────────────────────────────────


def test_empty_spec_emits_empty_string():
    sql, params = spec_to_sql(Spec(), table="t", allowed_cols={"a"})
    assert sql == ""
    assert params == ()


def test_single_where_clause():
    sql, params = spec_to_sql(
        Spec(where=[("user_id", "=", "u1")]),
        table="notifications",
        allowed_cols={"user_id"},
    )
    assert sql == "WHERE user_id = ?"
    assert params == ("u1",)


def test_multiple_where_clauses_join_with_and():
    sql, params = spec_to_sql(
        Spec(
            where=[
                ("user_id", "=", "u1"),
                ("created_at", "<", "2026-01-01"),
            ]
        ),
        table="t",
        allowed_cols={"user_id", "created_at"},
    )
    assert sql == "WHERE user_id = ? AND created_at < ?"
    assert params == ("u1", "2026-01-01")


def test_order_by_single_column():
    sql, params = spec_to_sql(
        Spec(order_by=[("created_at", "DESC")]),
        table="t",
        allowed_cols={"created_at"},
    )
    assert sql == "ORDER BY created_at DESC"
    assert params == ()


def test_limit_and_offset():
    sql, params = spec_to_sql(
        Spec(limit=20, offset=40),
        table="t",
        allowed_cols=set(),
    )
    assert sql == "LIMIT ? OFFSET ?"
    assert params == (20, 40)


def test_limit_zero_is_treated_as_unlimited():
    sql, _params = spec_to_sql(
        Spec(limit=0),
        table="t",
        allowed_cols=set(),
    )
    assert "LIMIT" not in sql


def test_full_spec_composes_in_order():
    sql, params = spec_to_sql(
        Spec(
            where=[("a", "=", 1)],
            order_by=[("a", "ASC")],
            limit=5,
        ),
        table="t",
        allowed_cols={"a"},
    )
    assert sql == "WHERE a = ? ORDER BY a ASC LIMIT ?"
    assert params == (1, 5)


# ─── Allow-list defends injection ────────────────────────────────────────


def test_disallowed_where_column_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        spec_to_sql(
            Spec(where=[("password", "=", "x")]),
            table="users",
            allowed_cols={"username"},
        )


def test_disallowed_order_column_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        spec_to_sql(
            Spec(order_by=[("password", "ASC")]),
            table="users",
            allowed_cols={"username"},
        )


def test_disallowed_op_rejected():
    with pytest.raises(ValueError, match="operator"):
        spec_to_sql(
            Spec(where=[("a", "JOIN", 1)]),
            table="t",
            allowed_cols={"a"},
        )


def test_disallowed_direction_rejected():
    with pytest.raises(ValueError, match="direction"):
        spec_to_sql(
            Spec(order_by=[("a", "RANDOM")]),
            table="t",
            allowed_cols={"a"},
        )


# ─── Repo integration — notifications ────────────────────────────────────


@pytest.fixture
async def notif_db(tmp_dir):
    from socialhome.crypto import (
        derive_instance_id,
        generate_identity_keypair,
    )
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "n.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('a','a-id','A')",
    )
    yield db
    await db.shutdown()


async def test_notification_find_by_type(notif_db):
    from socialhome.repositories.notification_repo import (
        SqliteNotificationRepo,
        new_notification,
    )

    repo = SqliteNotificationRepo(notif_db)
    await repo.save(
        new_notification(
            user_id="a-id",
            type="post_created",
            title="X",
        )
    )
    await repo.save(
        new_notification(
            user_id="a-id",
            type="task_assigned",
            title="Y",
        )
    )

    spec = Spec(
        where=[("user_id", "=", "a-id"), ("type", "=", "task_assigned")],
        order_by=[("created_at", "DESC")],
        limit=10,
    )
    out = await repo.find(spec)
    assert len(out) == 1
    assert out[0].type == "task_assigned"


async def test_notification_list_still_works_via_spec_internally(notif_db):
    """The bespoke list() method now composes a Spec internally — the
    public contract is unchanged."""
    from socialhome.repositories.notification_repo import (
        SqliteNotificationRepo,
        new_notification,
    )

    repo = SqliteNotificationRepo(notif_db)
    for i in range(3):
        await repo.save(
            new_notification(
                user_id="a-id",
                type="post_created",
                title=f"#{i}",
            )
        )

    out = await repo.list("a-id", limit=10)
    assert [n.title for n in out] == ["#2", "#1", "#0"]
