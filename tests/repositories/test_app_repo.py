"""Tests for SqliteAppRepo (installed_apps table).

Fixture choices
---------------
* Uses the shared ``db`` fixture from ``tests/conftest.py`` — a
  fully-migrated ``AsyncDatabase`` with ``PRAGMA foreign_keys=ON``.
* ``installed_by=None`` throughout: FK enforcement is ON in the test DB
  (confirmed in ``AsyncDatabase.startup``), so inserting a bare user-id
  string that has no matching ``users`` row would raise an integrity error.
  ``NULL`` is the honest representation of "installed without a tracked user"
  and is explicitly allowed by the schema (``TEXT REFERENCES … ON DELETE SET
  NULL``).

Enqueue ordering
----------------
``AsyncDatabase.enqueue`` awaits the future that the writer resolves once the
batch is committed, so a subsequent ``fetchone`` / ``fetchall`` always sees the
committed row — no extra flush or sleep is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.apps import AppManifest, AppPendingSession, InstalledApp
from socialhome.domain.user import User
from socialhome.repositories.app_repo import (
    _PENDING_SESSION_MAX_PER_PAIR,
    SqliteAppRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo


def _app(
    app_id: str = "chess", version: str = "1.0.0", enabled: bool = True
) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version=version,
        enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path=f"apps/{app_id}/{version}",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess.tgz",
        installed_by=None,  # FK enforcement is ON; no real user row available
        installed_at="2026-06-02T00:00:00+00:00",
    )


async def _seed_user(db) -> str:
    """Insert a minimal local user row and return its user_id."""
    user_id = uuid.uuid4().hex
    username = f"testuser_{user_id[:8]}"
    user = User(user_id=user_id, username=username, display_name="Test User")
    await SqliteUserRepo(db).save(user)
    return user_id


@pytest.mark.asyncio
async def test_install_and_get_roundtrip(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    got = await repo.get("chess")
    assert got is not None
    assert got.name == "Chess"
    assert got.manifest.entry == "index.html"
    assert got.enabled is True


@pytest.mark.asyncio
async def test_get_missing_returns_none(db):
    repo = SqliteAppRepo(db)
    assert await repo.get("nope") is None


@pytest.mark.asyncio
async def test_list_installed(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app(app_id="chess"))
    await repo.install(_app(app_id="notes"))
    rows = await repo.list_installed()
    assert {r.app_id for r in rows} == {"chess", "notes"}


@pytest.mark.asyncio
async def test_set_enabled_and_uninstall(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    await repo.set_enabled("chess", enabled=False)
    assert (await repo.get("chess")).enabled is False
    await repo.uninstall("chess")
    assert await repo.get("chess") is None


# ── KV store tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kv_set_get_roundtrip(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set(
        "chess", uid, "game:1", '{"turn":"w"}', "2026-06-02T00:00:00+00:00"
    )
    got = await repo.kv_get("chess", uid, "game:1")
    assert got is not None and got.value_json == '{"turn":"w"}'


@pytest.mark.asyncio
async def test_kv_set_upserts(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "k", '"a"', "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", uid, "k", '"b"', "2026-06-02T00:01:00+00:00")
    assert (await repo.kv_get("chess", uid, "k")).value_json == '"b"'
    assert await repo.kv_count("chess", uid) == 1


@pytest.mark.asyncio
async def test_kv_list_and_delete(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "a", "1", "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", uid, "b", "2", "2026-06-02T00:00:00+00:00")
    assert [e.key for e in await repo.kv_list("chess", uid)] == ["a", "b"]
    await repo.kv_delete("chess", uid, "a")
    assert {e.key for e in await repo.kv_list("chess", uid)} == {"b"}


@pytest.mark.asyncio
async def test_kv_cascades_on_uninstall(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "k", "1", "2026-06-02T00:00:00+00:00")
    await repo.uninstall("chess")
    assert await repo.kv_get("chess", uid, "k") is None
    assert await repo.kv_count("chess", uid) == 0


@pytest.mark.asyncio
async def test_kv_cascades_on_user_delete(db):
    """Deleting a user row must cascade-delete all their app_kv entries.

    Proves the ``FOREIGN KEY (user_id) REFERENCES users(user_id)
    ON DELETE CASCADE`` constraint on the ``app_kv`` table.
    """
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set(
        "chess", uid, "game:1", '{"turn":"w"}', "2026-06-02T00:00:00+00:00"
    )
    # Confirm the row is there before deletion.
    assert await repo.kv_count("chess", uid) == 1

    # Hard-delete the user row; no user_repo.delete method exists so we use
    # direct SQL (acceptable: this is a repo-layer test probing the DB schema).
    await db.enqueue("DELETE FROM users WHERE user_id = ?", (uid,))

    # The cascade must have removed the kv row.
    assert await repo.kv_count("chess", uid) == 0
    assert await repo.kv_get("chess", uid, "game:1") is None


# ── Pending-session invite store tests ───────────────────────────────────


def _pending(
    uid: str,
    session_id: str = "sess-1",
    *,
    payload: dict | None = None,
    created_at: str | None = None,
    from_user: str | None = "alice@remote",
) -> AppPendingSession:
    return AppPendingSession(
        app_id="chess",
        user_id=uid,
        session_id=session_id,
        from_instance="remote.example",
        from_user=from_user,
        payload=payload if payload is not None else {"kind": "challenge", "color": "w"},
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )


@pytest.mark.asyncio
async def test_pending_add_and_drain_roundtrip(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, payload={"kind": "challenge", "n": 3}))
    drained = await repo.drain_pending_sessions("chess", uid)
    assert len(drained) == 1
    s = drained[0]
    assert s.session_id == "sess-1"
    assert s.from_instance == "remote.example"
    assert s.from_user == "alice@remote"
    assert s.payload == {"kind": "challenge", "n": 3}


@pytest.mark.asyncio
async def test_pending_drain_deletes(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    assert len(await repo.drain_pending_sessions("chess", uid)) == 1
    # Second drain returns nothing — the first call consumed (deleted) the row.
    assert await repo.drain_pending_sessions("chess", uid) == []


@pytest.mark.asyncio
async def test_pending_drain_excludes_and_prune_deletes_expired(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old", created_at=old))
    fresh = _pending(uid, session_id="fresh")
    await repo.add_pending_session(fresh)

    # Drain with a small TTL: only the fresh row is returned; the old one is
    # excluded but still drained (deleted) for that (app, user) pair.
    drained = await repo.drain_pending_sessions("chess", uid, max_age_seconds=300)
    assert [s.session_id for s in drained] == ["fresh"]
    # Both rows are gone after drain.
    assert await repo.drain_pending_sessions("chess", uid) == []


@pytest.mark.asyncio
async def test_pending_prune_deletes_old_across_users(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="fresh"))

    await repo.prune_pending_sessions(max_age_seconds=300)
    # Prune removed the old row; the fresh one survives and still drains.
    drained = await repo.drain_pending_sessions("chess", uid)
    assert [s.session_id for s in drained] == ["fresh"]


@pytest.mark.asyncio
async def test_pending_pk_idempotent_latest_payload(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, payload={"v": 1}))
    await repo.add_pending_session(_pending(uid, payload={"v": 2}))
    drained = await repo.drain_pending_sessions("chess", uid)
    assert len(drained) == 1
    assert drained[0].payload == {"v": 2}


@pytest.mark.asyncio
async def test_pending_session_is_only_refreshed_by_its_own_household(db):
    """Another household reusing a pending invite's id changes nothing."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, payload={"v": 1}))
    theirs = _pending(uid, payload={"v": 3})
    await repo.add_pending_session(
        AppPendingSession(
            app_id=theirs.app_id,
            user_id=theirs.user_id,
            session_id=theirs.session_id,
            from_instance="elsewhere.example",
            from_user="eve",
            payload=theirs.payload,
            created_at=theirs.created_at,
        )
    )
    (s,) = await repo.drain_pending_sessions("chess", uid)
    assert (s.from_instance, s.payload) == ("remote.example", {"v": 1})


@pytest.mark.asyncio
async def test_pending_from_user_nullable(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, from_user=None))
    drained = await repo.drain_pending_sessions("chess", uid)
    assert drained[0].from_user is None


@pytest.mark.asyncio
async def test_pending_cascades_on_user_delete(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    await db.enqueue("DELETE FROM users WHERE user_id = ?", (uid,))
    assert await repo.drain_pending_sessions("chess", uid) == []


@pytest.mark.asyncio
async def test_pending_per_pair_cap_drops_oldest_on_insert(db):
    """A paired-peer flood is bounded: inserting more than the per-pair cap
    leaves exactly the cap rows, with the OLDEST dropped (newest survive)."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())

    cap = _PENDING_SESSION_MAX_PER_PAIR
    overflow = 5
    # Recent timestamps (within the drain TTL) spread by seconds so ordering
    # is deterministic: sess-0 is oldest, sess-(cap+overflow-1) is newest.
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(cap + overflow):
        created = (base + timedelta(seconds=i)).isoformat()
        await repo.add_pending_session(
            _pending(uid, session_id=f"sess-{i}", created_at=created)
        )

    drained = await repo.drain_pending_sessions("chess", uid)
    assert len(drained) == cap
    survivors = {s.session_id for s in drained}
    # The first ``overflow`` (oldest) sessions were pruned away.
    expected = {f"sess-{i}" for i in range(overflow, cap + overflow)}
    assert survivors == expected


@pytest.mark.asyncio
async def test_pending_cap_is_per_pair_not_global(db):
    """The cap is keyed by (app, user) — a second user is unaffected."""
    repo = SqliteAppRepo(db)
    uid_a = await _seed_user(db)
    uid_b = await _seed_user(db)
    await repo.install(_app())

    cap = _PENDING_SESSION_MAX_PER_PAIR
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(cap + 3):
        created = (base + timedelta(seconds=i)).isoformat()
        await repo.add_pending_session(
            _pending(uid_a, session_id=f"a-{i}", created_at=created)
        )
    # One row for user B survives untouched despite A overflowing.
    await repo.add_pending_session(_pending(uid_b, session_id="b-only"))

    assert len(await repo.drain_pending_sessions("chess", uid_a)) == cap
    drained_b = await repo.drain_pending_sessions("chess", uid_b)
    assert [s.session_id for s in drained_b] == ["b-only"]


@pytest.mark.asyncio
async def test_prune_pending_sessions_returns_deleted_count(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old1", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="old2", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="fresh"))

    deleted = await repo.prune_pending_sessions(max_age_seconds=300)
    assert deleted == 2
    # Nothing left to prune the second time.
    assert await repo.prune_pending_sessions(max_age_seconds=300) == 0
