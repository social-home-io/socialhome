"""Release-blocker protocol tests: how long a moment lives, and what a relay
passes on (v_65).

Marked ``@pytest.mark.security``.

An author chooses how long followers keep their moments (1–30 days), carried
as the moment's ``expires_at`` under the origin signature. A signature
proves who chose a value, not that it is reasonable, and a month-long moment
gives a relay a month to replay it. The rules these tests encode, against
the real application registry and SQLite:

* A received ``expires_at`` is kept at most ``created_at + 30 days``; one in
  range is kept byte for byte. The relayed envelope is forwarded verbatim
  (the clamp is local — the origin signature must still verify downstream).
* A create whose clamped expiry has passed is refused and not relayed.
* A household keeps a create signed more than 48 h ago but never relays it.
* A delete travels on only when it changed something here — a replayed
  delete for a tombstone already held goes nowhere, even after the relay
  seen-map forgot it (restart / TTL).
* A delete for a moment never held is held 30 days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.domain.moment import MOMENT_MAX_LIFETIME_DAYS
from socialhome.services import moment_federation_outbound as outbound_mod

from .test_moment_origin_signature import (
    _create,
    _delete,
    _relayed,
    _send,
    _signed,
    env,  # noqa: F401 — pytest fixture
)

pytestmark = pytest.mark.security

FET = FederationEventType


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


def _ahead(**kw) -> str:
    return (datetime.now(UTC) + timedelta(**kw)).isoformat()


async def _row(db, moment_id: str):
    return await db.fetchone(
        "SELECT created_at, expires_at, deleted_at FROM moments WHERE id=?",
        (moment_id,),
    )


async def test_a_year_long_expiry_is_kept_thirty_days(env):  # noqa: F811
    app, db, sent = env
    created = _ago(minutes=1)
    payload = _signed(
        FET.MOMENT_CREATED,
        _create(occurred_at=created, expires_at=_ahead(days=365)),
    )
    await _send(app, FET.MOMENT_CREATED, payload)
    row = await _row(db, "m-new")
    assert row is not None and row["deleted_at"] is None
    kept = datetime.fromisoformat(row["expires_at"])
    assert kept == datetime.fromisoformat(created) + timedelta(
        days=MOMENT_MAX_LIFETIME_DAYS
    )
    # Forwarded as signed — the clamp is this household's, not the wire's.
    onward = _relayed(sent)
    assert [s[1] for s in onward] == [FET.MOMENT_CREATED]
    assert onward[0][2]["expires_at"] == payload["expires_at"]


async def test_an_author_chosen_expiry_in_range_is_kept_verbatim(env):  # noqa: F811
    app, db, _sent = env
    expires = _ahead(days=21)
    await _send(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, _create(expires_at=expires)),
    )
    assert (await _row(db, "m-new"))["expires_at"] == expires


async def test_a_create_past_its_clamped_expiry_is_refused(env):  # noqa: F811
    """Signed 31 days ago with a far-future expiry: clamped, it ended a day
    ago — refused and not relayed."""
    app, db, sent = env
    await _send(
        app,
        FET.MOMENT_CREATED,
        _signed(
            FET.MOMENT_CREATED,
            _create(occurred_at=_ago(days=31), expires_at=_ahead(days=300)),
        ),
    )
    assert await _row(db, "m-new") is None
    assert _relayed(sent) == []


async def test_a_create_signed_over_48_hours_ago_is_kept_not_relayed(env):  # noqa: F811
    app, db, sent = env
    await _send(
        app,
        FET.MOMENT_CREATED,
        _signed(
            FET.MOMENT_CREATED,
            _create(occurred_at=_ago(hours=49), expires_at=_ahead(days=10)),
        ),
    )
    row = await _row(db, "m-new")
    assert row is not None and row["deleted_at"] is None
    assert _relayed(sent) == []


async def test_a_create_inside_48_hours_is_relayed(env):  # noqa: F811
    app, _db, sent = env
    await _send(
        app,
        FET.MOMENT_CREATED,
        _signed(
            FET.MOMENT_CREATED,
            _create(occurred_at=_ago(hours=47), expires_at=_ahead(days=10)),
        ),
    )
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_CREATED]


async def test_a_replayed_delete_is_not_relayed_again(env, monkeypatch):  # noqa: F811
    app, db, sent = env
    delete = _signed(FET.MOMENT_DELETED, _delete())  # m-olga, held here
    await _send(app, FET.MOMENT_DELETED, delete)
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_DELETED]
    assert (await _row(db, "m-olga"))["deleted_at"] is not None
    sent.clear()
    # The seen-map forgot it (a restart, or past its TTL): only the
    # "changed nothing here" rule stops the replay now.
    monkeypatch.setattr(outbound_mod, "RELAY_SEEN_TTL_S", -1.0)
    for _ in range(3):
        await _send(app, FET.MOMENT_DELETED, delete)
    assert _relayed(sent) == []


async def test_a_never_seen_delete_is_held_thirty_days_and_relayed_once(
    env,  # noqa: F811
    monkeypatch,
):
    app, db, sent = env
    monkeypatch.setattr(outbound_mod, "RELAY_SEEN_TTL_S", -1.0)
    delete = _signed(FET.MOMENT_DELETED, _delete(moment_id="m-unseen"))
    await _send(app, FET.MOMENT_DELETED, delete)
    await _send(app, FET.MOMENT_DELETED, delete)
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_DELETED]
    held = datetime.fromisoformat((await _row(db, "m-unseen"))["expires_at"])
    want = datetime.now(UTC) + timedelta(days=MOMENT_MAX_LIFETIME_DAYS)
    assert abs((held - want).total_seconds()) < 60
