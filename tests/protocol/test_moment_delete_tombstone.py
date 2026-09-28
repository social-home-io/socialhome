"""Release-blocker protocol tests: a deleted moment stays deleted.

Marked ``@pytest.mark.security``.

Origin signatures (v_35) prove *who* made a moment, not *when*: a relay can
hold on to a genuinely signed ``MOMENT_CREATED`` and re-send it after the
origin deleted the moment, and relays deliver out of order, so a signed
delete can land before its create. The rule these tests encode, against
the real application registry and SQLite:

    A verified ``MOMENT_DELETED`` leaves a tombstone on the ``moments`` row
    (content wiped, ``deleted_at`` set) that lives until the moment's
    ``expires_at`` — or, for a moment this household never stored, the
    maximum moment lifetime. While the tombstone exists a ``MOMENT_CREATED``
    for that id is refused and not relayed onward. Tombstones survive a
    restart and are swept by the retention scheduler once expired; a
    create that is already past its ``expires_at`` is refused too, so the
    sweep never re-opens the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, moment_service_key
from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.services.moment_service import MomentNotFoundError

from .test_moment_origin_signature import (
    NEW,
    ONWARD,
    ORIGIN,
    ORIGIN_KEY,
    RELAY,
    RELAY_KEY,
    _config,
    _create,
    _delete,
    _relayed,
    _seed_peer,
    _seed_user,
    _send,
    _signed,
    env,  # noqa: F401 — pytest fixture
)

pytestmark = pytest.mark.security

FET = FederationEventType


async def _live(db) -> set[str]:
    rows = await db.fetchall("SELECT id FROM moments WHERE deleted_at IS NULL", ())
    return {r[0] for r in rows}


async def _row(db, moment_id: str):
    return await db.fetchone(
        "SELECT content, media_url, deleted_at, expires_at FROM moments WHERE id=?",
        (moment_id,),
    )


def _signed_create(moment_id: str = "m-new", **extra) -> dict:
    return _signed(FET.MOMENT_CREATED, _create(moment_id=moment_id, **extra))


def _signed_delete(moment_id: str = "m-new") -> dict:
    return _signed(FET.MOMENT_DELETED, _delete(moment_id=moment_id))


async def test_replayed_signed_create_after_delete_is_refused_and_not_relayed(env):  # noqa: F811
    app, db, sent = env
    create = _signed_create(media_url="https://peer-origin/media/pic.webp")
    await _send(app, FET.MOMENT_CREATED, create)
    await _send(app, FET.MOMENT_DELETED, _signed_delete())
    assert "m-new" not in await _live(db)
    tomb = await _row(db, "m-new")
    assert tomb["deleted_at"] is not None
    assert tomb["content"] == ""
    assert tomb["media_url"] is None
    sent.clear()

    await _send(app, FET.MOMENT_CREATED, create)

    assert "m-new" not in await _live(db)
    assert (await _row(db, "m-new"))["content"] == ""
    assert sent == []


async def test_replay_delivered_directly_by_the_origin_is_refused_too(env):  # noqa: F811
    app, db, sent = env
    create = _create()
    await _send(app, FET.MOMENT_CREATED, create, from_instance=ORIGIN)
    await _send(app, FET.MOMENT_DELETED, _delete("m-new"), from_instance=ORIGIN)
    sent.clear()
    await _send(app, FET.MOMENT_CREATED, create, from_instance=ORIGIN)
    assert "m-new" not in await _live(db)
    assert sent == []


async def test_delete_before_create_leaves_the_moment_absent(env):  # noqa: F811
    app, db, sent = env
    await _send(app, FET.MOMENT_DELETED, _signed_delete())
    # The held delete still travels on for households that hold the moment.
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_DELETED]
    tomb = await _row(db, "m-new")
    assert tomb is not None and tomb["deleted_at"] is not None
    # Capped at the maximum moment lifetime — the create is unknown here.
    expires = datetime.fromisoformat(tomb["expires_at"])
    assert expires <= datetime.now(timezone.utc) + timedelta(days=7, minutes=1)
    sent.clear()

    await _send(app, FET.MOMENT_CREATED, _signed_create())

    assert "m-new" not in await _live(db)
    assert sent == []


async def test_a_tombstone_still_binds_its_author(env):  # noqa: F811
    """A delete pins the id to its author + origin even with no content."""
    app, db, sent = env
    await _send(app, FET.MOMENT_DELETED, _signed_delete())
    sent.clear()
    forged = _signed(
        FET.MOMENT_CREATED,
        _create(moment_id="m-new", author="u-rita", origin=RELAY),
        key=RELAY_KEY,
    )
    await _send(app, FET.MOMENT_CREATED, forged, from_instance=RELAY)
    assert "m-new" not in await _live(db)
    assert sent == []


async def test_tombstone_survives_a_restart(aiohttp_client, tmp_dir, monkeypatch):
    sent: list = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)

    first = create_app(_config(tmp_dir))
    client = await aiohttp_client(first)
    db = first[db_key]
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key, NEW)
    await _seed_peer(db, RELAY, RELAY_KEY.public_key, NEW)
    await _seed_peer(db, ONWARD, generate_identity_keypair().public_key, NEW)
    await _seed_user(db, "u-olga", ORIGIN)
    await _send(first, FET.MOMENT_DELETED, _signed_delete())
    await client.close()

    second = create_app(_config(tmp_dir))
    await aiohttp_client(second)
    sent.clear()
    await _send(second, FET.MOMENT_CREATED, _signed_create())

    assert "m-new" not in await _live(second[db_key])
    assert sent == []


async def test_expired_tombstone_is_purged_and_an_expired_replay_is_refused(env):  # noqa: F811
    app, db, sent = env
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _send(app, FET.MOMENT_DELETED, _signed_delete("m-olga"))
    await db.enqueue("UPDATE moments SET expires_at=? WHERE id='m-olga'", (past,))

    await app[moment_service_key].expire_due()

    assert await _row(db, "m-olga") is None
    sent.clear()
    # The only create that can still be replayed is one past its lifetime.
    await _send(app, FET.MOMENT_CREATED, _signed_create("m-olga", expires_at=past))
    assert await _row(db, "m-olga") is None
    assert sent == []


async def test_honest_create_then_delete_is_unchanged(env):  # noqa: F811
    app, db, sent = env
    await _send(app, FET.MOMENT_CREATED, _signed_create())
    assert "m-new" in await _live(db)
    await _send(app, FET.MOMENT_DELETED, _signed_delete())
    assert "m-new" not in await _live(db)
    assert [s[1] for s in _relayed(sent)] == [
        FET.MOMENT_CREATED,
        FET.MOMENT_DELETED,
    ]


async def test_local_author_delete_leaves_a_tombstone(env):  # noqa: F811
    app, db, _ = env
    svc = app[moment_service_key]
    moment = await svc.create_moment(author_user_id="u-anna", content="brb")
    await svc.delete_moment(moment.id, actor_user_id="u-anna")
    tomb = await _row(db, moment.id)
    assert tomb is not None and tomb["deleted_at"] is not None
    assert tomb["content"] == ""
    assert moment.id not in await _live(db)
    with pytest.raises(MomentNotFoundError):
        await svc.delete_moment(moment.id, actor_user_id="u-anna")
