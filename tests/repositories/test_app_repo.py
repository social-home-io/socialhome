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

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

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
async def test_the_bundles_i18n_names_are_kept_with_the_install(db):
    """A game row names the app in the reader's language right after a
    restart: the bundle's own names live in ``manifest_json`` (install
    and update), no column of their own."""
    repo = SqliteAppRepo(db)
    names = (("de", "Schiffe versenken"), ("nl", "Zeeslag"))
    app = _app()
    await repo.install(
        dataclasses.replace(
            app, manifest=dataclasses.replace(app.manifest, names=names)
        )
    )
    got = await repo.get("chess")
    assert got is not None and got.manifest.names == names
    await repo.update_installed(
        dataclasses.replace(
            got, manifest=dataclasses.replace(got.manifest, names=names[:1])
        )
    )
    again = await repo.get("chess")
    assert again is not None and again.manifest.names == names[:1]
    assert again.manifest.capabilities == ("storage",)


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
        created_at=created_at or datetime.now(UTC).isoformat(),
    )


async def _waiting(db, uid: str) -> list[AppPendingSession]:
    """The invites that wait for *uid* (oldest first) — read through the
    repo's own one-invite lookup."""
    repo = SqliteAppRepo(db)
    rows = await db.fetchall(
        "SELECT session_id FROM app_pending_sessions"
        " WHERE app_id = 'chess' AND user_id = ? ORDER BY created_at ASC",
        (uid,),
    )
    out = []
    for r in rows:
        p = await repo.get_pending_session("chess", uid, r["session_id"])
        assert p is not None
        out.append(p)
    return out


@pytest.mark.asyncio
async def test_pending_add_and_read_roundtrip(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, payload={"kind": "challenge", "n": 3}))
    drained = await _waiting(db, uid)
    assert len(drained) == 1
    s = drained[0]
    assert s.session_id == "sess-1"
    assert s.from_instance == "remote.example"
    assert s.from_user == "alice@remote"
    assert s.payload == {"kind": "challenge", "n": 3}


@pytest.mark.asyncio
async def test_pending_prune_deletes_old_across_users(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="fresh"))

    await repo.prune_pending_sessions(max_age_seconds=300)
    # Prune removed the old row; the fresh one survives.
    drained = await _waiting(db, uid)
    assert [s.session_id for s in drained] == ["fresh"]


@pytest.mark.asyncio
async def test_pending_pk_idempotent_latest_payload(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, payload={"v": 1}))
    await repo.add_pending_session(_pending(uid, payload={"v": 2}))
    drained = await _waiting(db, uid)
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
    (s,) = await _waiting(db, uid)
    assert (s.from_instance, s.payload) == ("remote.example", {"v": 1})


@pytest.mark.asyncio
async def test_pending_from_user_nullable(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, from_user=None))
    drained = await _waiting(db, uid)
    assert drained[0].from_user is None


@pytest.mark.asyncio
async def test_pending_cascades_on_user_delete(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    await db.enqueue("DELETE FROM users WHERE user_id = ?", (uid,))
    assert await _waiting(db, uid) == []


@pytest.mark.asyncio
async def test_pending_per_pair_cap_drops_oldest_on_insert(db):
    """A paired-peer flood is bounded: inserting more than the per-pair cap
    leaves exactly the cap rows, with the OLDEST dropped (newest survive)."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())

    cap = _PENDING_SESSION_MAX_PER_PAIR
    overflow = 5
    # Recent timestamps (within the TTL) spread by seconds so ordering
    # is deterministic: sess-0 is oldest, sess-(cap+overflow-1) is newest.
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(cap + overflow):
        created = (base + timedelta(seconds=i)).isoformat()
        await repo.add_pending_session(
            _pending(uid, session_id=f"sess-{i}", created_at=created)
        )

    drained = await _waiting(db, uid)
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
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(cap + 3):
        created = (base + timedelta(seconds=i)).isoformat()
        await repo.add_pending_session(
            _pending(uid_a, session_id=f"a-{i}", created_at=created)
        )
    # One row for user B survives untouched despite A overflowing.
    await repo.add_pending_session(_pending(uid_b, session_id="b-only"))

    assert len(await _waiting(db, uid_a)) == cap
    drained_b = await _waiting(db, uid_b)
    assert [s.session_id for s in drained_b] == ["b-only"]


@pytest.mark.asyncio
async def test_prune_pending_sessions_returns_deleted_count(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old1", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="old2", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="fresh"))

    deleted = await repo.prune_pending_sessions(max_age_seconds=300)
    assert deleted == 2
    # Nothing left to prune the second time.
    assert await repo.prune_pending_sessions(max_age_seconds=300) == 0


@pytest.mark.asyncio
async def test_get_pending_session_reads_one_invite_expired_or_not(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="old", created_at=old))
    got = await repo.get_pending_session("chess", uid, "old")
    assert got is not None and got.created_at == old
    assert await repo.get_pending_session("chess", uid, "nope") is None


@pytest.mark.asyncio
async def test_take_pending_session_removes_it_once(db):
    """The one step a decline, a withdrawal and the app taking it in race
    on: the first wins the row, the second gets ``None``."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    taken = await repo.take_pending_session("chess", uid, "sess-1")
    assert taken is not None and taken.from_user == "alice@remote"
    assert await repo.take_pending_session("chess", uid, "sess-1") is None
    assert await repo.get_pending_session("chess", uid, "sess-1") is None


@pytest.mark.asyncio
async def test_take_pending_session_only_for_who_sent_it(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    assert (
        await repo.take_pending_session(
            "chess", uid, "sess-1", from_instance="other.example"
        )
        is None
    )
    assert (
        await repo.take_pending_session(
            "chess", uid, "sess-1", from_instance="remote.example", from_user="eve"
        )
        is None
    )
    assert await repo.get_pending_session("chess", uid, "sess-1") is not None
    taken = await repo.take_pending_session(
        "chess",
        uid,
        "sess-1",
        from_instance="remote.example",
        from_user="alice@remote",
    )
    assert taken is not None


# ── Mailbox (0131, D11): active sessions and held messages ──────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_play_activates_the_invite_and_keeps_the_row(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    invite = await repo.activate_pending_session("chess", uid, "sess-1", now=_now())
    assert invite is not None and invite.state == "invited"
    row = await repo.get_pending_session("chess", uid, "sess-1")
    assert row is not None and row.state == "active" and row.updated_at
    # Played once: a second Play, a decline or a withdrawal finds nothing.
    assert (
        await repo.activate_pending_session("chess", uid, "sess-1", now=_now()) is None
    )
    assert await repo.take_pending_session("chess", uid, "sess-1") is None
    assert (
        await repo.take_pending_session(
            "chess",
            uid,
            "sess-1",
            from_instance="remote.example",
            from_user="alice@remote",
        )
        is None
    )


@pytest.mark.asyncio
async def test_hold_keeps_only_the_latest_message_per_session(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    for n in (1, 2, 3):
        assert await repo.hold_message(
            "chess",
            uid,
            "sess-1",
            from_instance="remote.example",
            from_user="alice@remote",
            held={"kind": "message", "payload": {"n": n}},
            now=_now(),
        )
    # Kept while the invite waits, handed over only once it is played (D3).
    assert await repo.take_held("chess", uid) == []
    await repo.activate_pending_session("chess", uid, "sess-1", now=_now())
    taken = await repo.take_held("chess", uid)
    assert [t.held for t in taken] == [{"kind": "message", "payload": {"n": 3}}]
    # Cleared: a second open gets nothing, the session stays known.
    assert await repo.take_held("chess", uid) == []
    assert await repo.get_pending_session("chess", uid, "sess-1") is not None


@pytest.mark.asyncio
async def test_hold_refuses_an_unknown_session_or_another_sender(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    held = {"kind": "message", "payload": {}}
    for sid, inst, user in (
        ("nope", "remote.example", "alice@remote"),
        ("sess-1", "other.example", "alice@remote"),
        ("sess-1", "remote.example", "eve"),
    ):
        assert not await repo.hold_message(
            "chess",
            uid,
            sid,
            from_instance=inst,
            from_user=user,
            held=held,
            now=_now(),
        )
    assert await repo.take_held("chess", uid) == []


@pytest.mark.asyncio
async def test_add_active_session_leaves_an_existing_row_alone(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_active_session(_pending(uid, session_id="mine", from_user="ben"))
    row = await repo.get_pending_session("chess", uid, "mine")
    assert row is not None and row.state == "active" and row.from_user == "ben"
    # An invite already known stays an invite, from who sent it.
    await repo.add_pending_session(_pending(uid))
    await repo.add_active_session(_pending(uid, from_user="eve"))
    row = await repo.get_pending_session("chess", uid, "sess-1")
    assert row is not None and row.state == "invited"
    assert row.from_user == "alice@remote"


@pytest.mark.asyncio
async def test_prune_keeps_a_game_with_recent_moves(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await repo.add_pending_session(_pending(uid, session_id="busy", created_at=old))
    await repo.add_pending_session(_pending(uid, session_id="quiet", created_at=old))
    await repo.touch_session("chess", uid, "busy", now=_now())
    assert await repo.prune_pending_sessions() == 1
    assert await repo.get_pending_session("chess", uid, "busy") is not None
    assert await repo.get_pending_session("chess", uid, "quiet") is None


@pytest.mark.asyncio
async def test_delete_pending_session_forgets_it(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    await repo.delete_pending_session("chess", uid, "sess-1")
    assert await repo.get_pending_session("chess", uid, "sess-1") is None


@pytest.mark.asyncio
async def test_uninstall_drops_held_messages(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid))
    await repo.hold_message(
        "chess",
        uid,
        "sess-1",
        from_instance="remote.example",
        from_user="alice@remote",
        held={"kind": "message", "payload": {}},
        now=_now(),
    )
    await repo.uninstall("chess")
    assert await repo.get_pending_session("chess", uid, "sess-1") is None


# ── D3: the invite sheet's session marks (R3 PR 9) ────────────────────────


def _sent(uid: str, session_id: str, **kw) -> AppPendingSession:
    """An invite *uid* sent through the sheet, to alice@remote."""
    return AppPendingSession(
        app_id="chess",
        user_id=uid,
        session_id=session_id,
        from_instance="remote.example",
        from_user="alice@remote",
        payload={"app_id": "chess", "session_id": session_id, "sent": True},
        created_at=_now(),
        state="active",
        updated_at=_now(),
        **kw,
    )


@pytest.mark.asyncio
async def test_an_answer_marks_a_sent_invite_answered(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_active_session(_sent(uid, "mine"))
    await repo.add_pending_session(_pending(uid))  # an invite *to* uid
    row = await repo.get_pending_session("chess", uid, "mine")
    assert row is not None and row.sent and not row.answered
    for sid in ("mine", "sess-1"):
        assert await repo.hold_message(
            "chess",
            uid,
            sid,
            from_instance="remote.example",
            from_user="alice@remote",
            held={"kind": "message", "payload": {}},
            now=_now(),
        )
    row = await repo.get_pending_session("chess", uid, "mine")
    assert row is not None and row.answered and row.sent
    # Only a sent invite gets the mark.
    other = await repo.get_pending_session("chess", uid, "sess-1")
    assert other is not None and "answered" not in other.payload


@pytest.mark.asyncio
async def test_withdraw_session_marks_it_and_keeps_the_row(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_active_session(_sent(uid, "mine"))
    await repo.withdraw_session("chess", uid, "mine")
    row = await repo.get_pending_session("chess", uid, "mine")
    assert row is not None and row.withdrawn and row.sent


@pytest.mark.asyncio
async def test_list_sessions_lists_one_persons_rows_latest_first(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await repo.add_pending_session(_pending(uid, created_at=old))
    await repo.add_active_session(_sent(uid, "mine"))
    assert [r.session_id for r in await repo.list_sessions("chess", uid)] == [
        "mine",
        "sess-1",
    ]
    assert await repo.list_sessions("chess", "someone-else") == []


@pytest.mark.asyncio
async def test_an_open_never_rewrites_a_game_already_sent_or_played(db):
    """An inbound open reusing the id of a game this person sent (or
    played) changes nothing — not its other side, not its marks."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_active_session(_sent(uid, "mine"))
    await repo.add_pending_session(
        AppPendingSession(
            app_id="chess",
            user_id=uid,
            session_id="mine",
            from_instance="remote.example",
            from_user="mallory@remote",
            payload={"verb": "open"},
            created_at=_now(),
        )
    )
    row = await repo.get_pending_session("chess", uid, "mine")
    assert row is not None
    assert (row.state, row.from_user, row.sent) == ("active", "alice@remote", True)


@pytest.mark.asyncio
async def test_a_flood_of_invites_never_pushes_out_a_game_or_a_sent_invite(db):
    """Eviction (50 per person and app) takes only waiting invites: a paired
    household can't wipe someone's games or reset their invite caps (D3)."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await repo.add_active_session(
        dataclasses.replace(_sent(uid, "mine"), created_at=old, updated_at=old)
    )
    for n in range(60):
        await repo.add_pending_session(_pending(uid, session_id=f"flood-{n}"))
    rows = await repo.list_sessions("chess", uid)
    assert len([r for r in rows if r.state == "invited"]) == 50
    mine = await repo.get_pending_session("chess", uid, "mine")
    assert mine is not None and mine.sent


@pytest.mark.asyncio
async def test_prune_keeps_a_game_90_days_and_an_invite_14(db):
    """Owner decision 2026-10-29: a game being played stays known 90 days
    after its last move; a waiting invite still goes after 14."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())

    def ago(days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    for sid, days in (("game-60", 60), ("game-91", 91)):
        await repo.add_active_session(
            dataclasses.replace(
                _sent(uid, sid), created_at=ago(days), updated_at=ago(days)
            )
        )
    await repo.add_pending_session(_pending(uid, "invite-10", created_at=ago(10)))
    await repo.add_pending_session(_pending(uid, "invite-15", created_at=ago(15)))
    assert await repo.prune_pending_sessions() == 2
    left = sorted(r.session_id for r in await repo.list_sessions("chess", uid))
    assert left == ["game-60", "invite-10"]


@pytest.mark.asyncio
async def test_an_invite_is_refreshed_only_by_the_same_person(db):
    """Another person of the same household reusing a waiting invite's
    session id can't re-point it at themselves."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, "w", from_user="alice@remote"))
    await repo.add_pending_session(_pending(uid, "w", from_user="mallory@remote"))
    row = await repo.get_pending_session("chess", uid, "w")
    assert row is not None and row.from_user == "alice@remote"


@pytest.mark.asyncio
async def test_list_app_sessions_lists_everyones_rows_of_one_app(db):
    """What an uninstall ends (D9): every person's sessions of this app."""
    repo = SqliteAppRepo(db)
    a = await _seed_user(db)
    b = await _seed_user(db)
    await repo.install(_app())
    await repo.install(_app("draughts"))
    await repo.add_pending_session(_pending(a))
    await repo.add_active_session(_sent(b, "mine"))
    await repo.add_active_session(
        dataclasses.replace(_sent(b, "other-app"), app_id="draughts")
    )
    rows = await repo.list_app_sessions("chess")
    assert sorted((r.user_id, r.session_id) for r in rows) == sorted(
        [(a, "sess-1"), (b, "mine")]
    )
    assert await repo.list_app_sessions("nope") == []


@pytest.mark.asyncio
async def test_kv_people_counts_people_with_saved_data(db):
    repo = SqliteAppRepo(db)
    a = await _seed_user(db)
    b = await _seed_user(db)
    await repo.install(_app())
    assert await repo.kv_people("chess") == 0
    now = datetime.now(UTC).isoformat()
    await repo.kv_set("chess", a, "game:1", "{}", now)
    await repo.kv_set("chess", a, "game:2", "{}", now)
    await repo.kv_set("chess", b, "settings", "{}", now)
    assert await repo.kv_people("chess") == 2
    assert await repo.kv_people("draughts") == 0


@pytest.mark.asyncio
async def test_a_group_seat_and_a_one_to_one_invite_never_refresh_each_other(db):
    """v_76: the same household and person reusing a session id can't turn
    a group seat into a 1:1 invite (dropping its ``group`` mark), nor the
    other way round."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, "g1", payload={"group": True}))
    await repo.add_pending_session(_pending(uid, "g1", payload={"verb": "open"}))
    assert (await repo.get_pending_session("chess", uid, "g1")).is_group
    await repo.add_pending_session(_pending(uid, "s1", payload={"verb": "open"}))
    await repo.add_pending_session(_pending(uid, "s1", payload={"group": True}))
    assert not (await repo.get_pending_session("chess", uid, "s1")).is_group


@pytest.mark.asyncio
async def test_hold_never_fills_a_group_seat(db):
    """A group seat's mailbox is per sender seat (v_76 part II): a 1:1 move
    on its session id from its host is never held in ``held_json``."""
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(uid, "g1", payload={"group": True}))
    assert not await repo.hold_message(
        "chess",
        uid,
        "g1",
        from_instance="remote.example",
        from_user="alice@remote",
        held={"kind": "message", "payload": {}},
        now=_now(),
    )


@pytest.mark.asyncio
async def test_list_session_rows_lists_everyone_at_one_session(db):
    repo = SqliteAppRepo(db)
    a = await _seed_user(db)
    b = await _seed_user(db)
    await repo.install(_app())
    await repo.add_pending_session(_pending(a, "g1", payload={"group": True}))
    await repo.add_pending_session(_pending(b, "g1", payload={"group": True}))
    await repo.add_pending_session(_pending(b, "other"))
    rows = await repo.list_session_rows("chess", "g1")
    assert sorted(r.user_id for r in rows) == sorted([a, b])
    assert await repo.list_session_rows("chess", "nope") == []
