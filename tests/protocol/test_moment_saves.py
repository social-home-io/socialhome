"""Release-blocker protocol tests: a saved moment still goes when its
author deletes it (owner decision 6b, sign-offs 3 + 5 of 2026-10-25).

Marked ``@pytest.mark.security``.

Saving keeps someone's moment past its expiry on this household — never
past its author's delete. The rule these tests encode, against the real
application registry (mesh) and the real GFS-inbound handler, on SQLite:

    A verified delete from the author — a ``MOMENT_DELETED`` over the mesh,
    or a signed delete certificate a GFS pushes on reconnect — wipes the
    saved copy and drops every save of it in the same transaction, also
    after the moment's expiry. A replayed create never brings the save back,
    and a forged delete changes nothing. Saving is offered only where such a
    delete can arrive: a paired v_66+ origin household, or a GFS that keeps
    delete certificates.
"""

from __future__ import annotations

import pytest

from socialhome.app_keys import moment_service_key
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.moment import MomentSaveUnavailableError
from socialhome.services.moment_service import MomentNotFoundError

from .test_moment_late_deletes import _cert, _follower, _hold, _push
from .test_moment_origin_signature import (
    ORIGIN,
    RELAY_KEY,
    _create,
    _delete,
    _send,
    _signed,
    env,  # noqa: F401 — pytest fixture
)

pytestmark = pytest.mark.security

FET = FederationEventType
V66 = FederationCapability.MIN_FOR_LATE_MOMENT_DELETES


async def _origin_on(db, version: int) -> None:
    await db.enqueue(
        "UPDATE remote_instances SET proto_version=? WHERE id=?", (version, ORIGIN)
    )


async def _saves(db) -> list[tuple[str, str]]:
    rows = await db.fetchall("SELECT user_id, moment_id FROM moment_saves", ())
    return [(r[0], r[1]) for r in rows]


async def _expire(db, moment_id: str) -> None:
    await db.enqueue(
        "UPDATE moments SET expires_at=datetime('now', '-1 hour') WHERE id=?",
        (moment_id,),
    )


async def test_the_authors_mesh_delete_removes_a_saved_copy(env):  # noqa: F811
    app, db, _sent = env
    await _origin_on(db, V66)
    svc = app[moment_service_key]
    create = _signed(FET.MOMENT_CREATED, _create(moment_id="m-new"))
    await _send(app, FET.MOMENT_CREATED, create)
    await svc.save_moment("m-new", user_id="u-anna")
    assert await _saves(db) == [("u-anna", "m-new")]
    # Past its expiry the saved copy is still Anna's...
    await _expire(db, "m-new")
    assert await svc.expire_due() == 0
    assert (await svc.get_for_viewer("m-new", "u-anna")).content
    # ...until the author deletes it.
    await _send(app, FET.MOMENT_DELETED, _signed(FET.MOMENT_DELETED, _delete("m-new")))
    assert await _saves(db) == []
    with pytest.raises(MomentNotFoundError):
        await svc.get_for_viewer("m-new", "u-anna")
    row = await db.fetchone("SELECT content, deleted_at FROM moments WHERE id='m-new'")
    assert row["content"] == "" and row["deleted_at"] is not None
    # A replayed create brings back neither the moment nor the save.
    await _send(app, FET.MOMENT_CREATED, create)
    assert await _saves(db) == []
    assert await svc.list_saved(user_id="u-anna") == []


async def test_a_forged_delete_leaves_the_saved_copy(env):  # noqa: F811
    app, db, _sent = env
    await _origin_on(db, V66)
    svc = app[moment_service_key]
    await _send(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, _create()))
    await svc.save_moment("m-new", user_id="u-anna")
    forged = _signed(FET.MOMENT_DELETED, _delete("m-new"), key=RELAY_KEY)
    await _send(app, FET.MOMENT_DELETED, forged)
    assert await _saves(db) == [("u-anna", "m-new")]


async def test_a_moment_from_an_older_household_is_not_saveable(env):  # noqa: F811
    """Its origin (v_65) gives up on a delete after ~10 h; a copy kept for
    weeks would outlive the author's wish — so Save isn't offered."""
    app, db, _sent = env
    await _origin_on(db, V66 - 1)
    await _send(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, _create()))
    with pytest.raises(MomentSaveUnavailableError):
        await app[moment_service_key].save_moment("m-new", user_id="u-anna")
    assert await _saves(db) == []


async def test_a_gfs_delete_certificate_removes_a_saved_copy(db):
    follower = await _follower(db)
    await _hold(follower)
    await db.enqueue(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
        " VALUES('u1', 'm-1', datetime('now'))"
    )
    await _expire(db, "m-1")
    assert await follower.moments.prune_expired() == 0
    await _push(follower, [_cert()])
    assert await _saves(db) == []
    assert await follower.moments.get_readable("m-1", "u1") is None


async def test_a_forged_certificate_leaves_the_saved_copy(db):
    follower = await _follower(db)
    await _hold(follower)
    await db.enqueue(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
        " VALUES('u1', 'm-1', datetime('now'))"
    )
    await _push(follower, [_cert(key=RELAY_KEY, origin=ORIGIN)])
    assert await _saves(db) == [("u1", "m-1")]
