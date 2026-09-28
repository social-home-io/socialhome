"""Release-blocker protocol tests: a moment can only be announced by the
household that created it (v_36).

Marked ``@pytest.mark.security``.

A moment id stored here binds the moment to its author and origin (the
stored row decides, ``_moment_row_binds``), and a delete leaves a tombstone
(v_35). So whichever household is first to name a moment id here decides
whose it is: a household that has seen another household's new moment id
could announce a create — or a delete — of its own under that id and have
the real moment refused. From v_36 a moment id is owner-bound to its
author (``federation/owner_bound_id.py``, the unscoped ``moment`` kind), so
such a claim is refused on sight: neither stored, nor tombstoned, nor
relayed onward. A legacy (uuid4) id keeps today's rules.
"""

from __future__ import annotations

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.federation.owner_bound_id import MOMENT_KIND, mint_owner_bound_id

from .test_moment_origin_signature import (
    ORIGIN,
    RELAY,
    _create,
    _delete,
    _moments,
    _relayed,
    _send,
    _signed,
    env,  # noqa: F401 — pytest fixture
)

pytestmark = pytest.mark.security

FET = FederationEventType

LEGACY_ID = "0123456789ab4def8123456789abcdef"  # a uuid4 hex, pre-binding


def _olgas() -> str:
    return mint_owner_bound_id(MOMENT_KIND, space_id="", owner_user_id="u-olga")


def _claim(moment_id: str) -> dict:
    """RELAY announces the id as a moment of its own user, u-rita."""
    return _create(moment_id=moment_id, author="u-rita", origin=RELAY)


@pytest.mark.parametrize("claim", ["create", "delete"])
async def test_a_racing_claim_is_refused_and_the_creators_moment_lands(
    env,  # noqa: F811
    claim,
    caplog,
):
    app, db, sent = env
    moment_id = _olgas()
    before = await _moments(db)
    with caplog.at_level("WARNING"):
        if claim == "create":
            await _send(app, FET.MOMENT_CREATED, _claim(moment_id))
        else:
            await _send(
                app,
                FET.MOMENT_DELETED,
                _delete(moment_id=moment_id, author="u-rita", origin=RELAY),
            )
    assert await _moments(db) == before  # nothing stored, nothing tombstoned
    assert _relayed(sent) == []
    assert "not bound to" in caplog.text
    await _send(
        app, FET.MOMENT_CREATED, _create(moment_id=moment_id), from_instance=ORIGIN
    )
    row = (await _moments(db))[moment_id]
    assert row[1] == "u-olga" and row[4] is None


async def test_a_relayed_bound_moment_keeps_it_valid(env):  # noqa: F811
    """The id binds the author, not the relay: a signed relay still lands."""
    app, db, _sent = env
    moment_id = _olgas()
    await _send(
        app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, _create(moment_id))
    )
    assert (await _moments(db))[moment_id][1] == "u-olga"


async def test_a_bound_moment_id_with_an_unknown_suite_is_refused(env):  # noqa: F811
    app, db, _sent = env
    good = _olgas()
    unknown = good[:16] + "b" + good[17:]
    await _send(app, FET.MOMENT_CREATED, _create(unknown), from_instance=ORIGIN)
    assert unknown not in await _moments(db)


async def test_the_creators_own_delete_still_sticks(env):  # noqa: F811
    """A delete from the author's household that overtakes its create is
    remembered, as before — only a claim for somebody else is refused."""
    app, db, _sent = env
    moment_id = _olgas()
    await _send(
        app, FET.MOMENT_DELETED, _delete(moment_id=moment_id), from_instance=ORIGIN
    )
    await _send(app, FET.MOMENT_CREATED, _create(moment_id), from_instance=ORIGIN)
    assert (await _moments(db))[moment_id][4] is not None


async def test_a_legacy_moment_id_keeps_the_first_come_rule(env):  # noqa: F811
    app, db, _sent = env
    await _send(app, FET.MOMENT_CREATED, _claim(LEGACY_ID))
    await _send(app, FET.MOMENT_CREATED, _create(LEGACY_ID), from_instance=ORIGIN)
    assert (await _moments(db))[LEGACY_ID][1] == "u-rita"
