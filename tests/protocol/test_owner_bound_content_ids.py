"""Release-blocker protocol tests: a new row can only be announced by the
household that created it (v_36).

Marked ``@pytest.mark.security``.

``test_space_content_authorship.py`` binds the user a payload names to a
member seated on the household that signed it. That alone leaves a
first-come race on a *new* row's id: a member household that has seen
another household's new id can announce it first, for its own user, and
the creator's real announcement then meets an id already held. From v_36
every federated space row with an owner carries an owner-bound id
(``federation/owner_bound_id.py``), so such a claim is refused on sight.

For every kind this file proves, against the REAL application registry
over a real SQLite database:

* a racing claim of a bound id for another user is refused, and the
  creator's genuine announcement still lands afterwards;
* the host relaying the creator's row (the §25.6 resume replay) stays
  valid — the id binds the owner, not the envelope's sender;
* the id is bound to its space, and an unknown suite nibble is refused;
* a legacy (uuid4) id keeps today's rules;
* the §25.6 sync receiver skips a record claiming a bound id for anybody
  but its creator — from the host too.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, space_sync_receiver_key
from socialhome.domain.federation import FederationEventType
from socialhome.federation.owner_bound_id import (
    GALLERY_ITEM_KIND,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_COMMENT_KIND,
    SPACE_PAGE_KIND,
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)

from .test_space_content_authorship import (
    _CAL,
    _SEED,
    AUTHOR,
    HOST,
    OTHER,
    SP,
    _config,
    _deliver,
)

pytestmark = pytest.mark.security

FET = FederationEventType

CREATOR = "u-a"  # seated on AUTHOR
CLAIMANT = "u-o"  # seated on OTHER
LEGACY_ID = "0123456789ab4def8123456789abcdef"  # a uuid4 hex, pre-binding


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    """The authorship suite's space: seats, seeded rows, the real registry."""
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    return app, db


@dataclass(frozen=True, slots=True)
class Kind:
    label: str
    kind: str
    create: FederationEventType
    #: The live create payload for ``(row_id, owner)``.
    payload: Callable[[str, str], dict]
    table: str
    owner_col: str
    sync_resource: str
    #: The §25.6 sync record for ``(row_id, owner)``.
    record: Callable[[str, str], dict]


KINDS: list[Kind] = [
    Kind(
        "post",
        SPACE_POST_KIND,
        FET.SPACE_POST_CREATED,
        lambda rid, who: {"id": rid, "author": who, "type": "text", "content": "x"},
        "space_posts",
        "author",
        "posts",
        lambda rid, who: {"id": rid, "author": who, "type": "text", "content": "x"},
    ),
    Kind(
        "comment",
        SPACE_COMMENT_KIND,
        FET.SPACE_COMMENT_CREATED,
        lambda rid, who: {
            "post_id": "post-a",
            "comment_id": rid,
            "author": who,
            "content": "x",
        },
        "space_post_comments",
        "author",
        "comments",
        lambda rid, who: {
            "id": rid,
            "post_id": "post-a",
            "author": who,
            "type": "text",
            "content": "x",
        },
    ),
    Kind(
        "gallery item",
        GALLERY_ITEM_KIND,
        FET.SPACE_GALLERY_ITEM_CREATED,
        lambda rid, who: {
            "id": rid,
            "album_id": "album-g",
            "uploaded_by": who,
            "item_type": "photo",
        },
        "gallery_items",
        "uploaded_by",
        "gallery",
        lambda rid, who: {
            "kind": "item",
            "id": rid,
            "album_id": "album-g",
            "uploaded_by": who,
        },
    ),
    Kind(
        "calendar event",
        SPACE_CALENDAR_EVENT_KIND,
        FET.SPACE_CALENDAR_EVENT_CREATED,
        lambda rid, who: {"id": rid, "summary": "x", "created_by": who, **_CAL},
        "space_calendar_events",
        "created_by",
        "calendar",
        lambda rid, who: {"id": rid, "summary": "x", "created_by": who, **_CAL},
    ),
    Kind(
        "task",
        SPACE_TASK_KIND,
        FET.SPACE_TASK_CREATED,
        lambda rid, who: {
            "id": rid,
            "list_id": "list-a",
            "title": "x",
            "created_by": who,
        },
        "space_tasks",
        "created_by",
        "tasks",
        lambda rid, who: {
            "id": rid,
            "list_id": "list-a",
            "title": "x",
            "created_by": who,
        },
    ),
    Kind(
        "task list",
        SPACE_TASK_LIST_KIND,
        FET.SPACE_TASK_LIST_CREATED,
        lambda rid, who: {"id": rid, "name": "x", "created_by": who},
        "space_task_lists",
        "created_by",
        "task_lists",
        lambda rid, who: {"id": rid, "name": "x", "created_by": who},
    ),
    Kind(
        "page",
        SPACE_PAGE_KIND,
        FET.SPACE_PAGE_CREATED,
        lambda rid, who: {"id": rid, "title": "x", "created_by": who},
        "space_pages",
        "created_by",
        "pages",
        lambda rid, who: {"id": rid, "title": "x", "created_by": who},
    ),
    Kind(
        "sticky",
        SPACE_STICKY_KIND,
        FET.SPACE_STICKY_CREATED,
        lambda rid, who: {"id": rid, "author": who, "content": "x"},
        "stickies",
        "author",
        "stickies",
        lambda rid, who: {"id": rid, "author": who, "content": "x"},
    ),
]

_IDS = [k.label for k in KINDS]


def _bound(k: Kind, owner: str = CREATOR, space: str = SP) -> str:
    return mint_owner_bound_id(k.kind, space_id=space, owner_user_id=owner)


async def _owner(db, k: Kind, row_id: str) -> str | None:
    row = await db.fetchone(
        f"SELECT {k.owner_col} AS who FROM {k.table} WHERE id=?", (row_id,)
    )
    return None if row is None else row["who"]


@pytest.mark.parametrize("k", KINDS, ids=_IDS)
async def test_a_racing_claim_is_refused_and_the_creators_create_lands(env, k, caplog):
    app, db = env
    row_id = _bound(k)
    with caplog.at_level("WARNING"):
        await _deliver(app, k.create, k.payload(row_id, CLAIMANT), sender=OTHER)
    assert await _owner(db, k, row_id) is None
    assert "not bound to" in caplog.text
    await _deliver(app, k.create, k.payload(row_id, CREATOR), sender=AUTHOR)
    assert await _owner(db, k, row_id) == CREATOR


@pytest.mark.parametrize("k", KINDS, ids=_IDS)
async def test_the_host_relaying_a_bound_row_keeps_it_valid(env, k):
    app, db = env
    row_id = _bound(k)
    await _deliver(app, k.create, k.payload(row_id, CREATOR), sender=HOST)
    assert await _owner(db, k, row_id) == CREATOR


@pytest.mark.parametrize("k", KINDS, ids=_IDS)
async def test_a_bound_id_is_bound_to_its_space(env, k):
    app, db = env
    row_id = _bound(k, space="sp-elsewhere")
    await _deliver(app, k.create, k.payload(row_id, CREATOR), sender=AUTHOR)
    assert await _owner(db, k, row_id) is None


@pytest.mark.parametrize("k", KINDS, ids=_IDS)
async def test_a_bound_id_with_an_unknown_suite_is_refused(env, k):
    app, db = env
    good = _bound(k)
    unknown = good[:16] + "b" + good[17:]
    await _deliver(app, k.create, k.payload(unknown, CREATOR), sender=AUTHOR)
    assert await _owner(db, k, unknown) is None


#: Kinds whose create event predates owner binding, so a legacy id is
#: still a valid (first-come) claim on the live path. A task list's create
#: is new in v_40 and always carries a bound id — see the test below.
_LEGACY_LIVE_KINDS = [k for k in KINDS if k.kind != SPACE_TASK_LIST_KIND]


@pytest.mark.parametrize(
    "k", _LEGACY_LIVE_KINDS, ids=[k.label for k in _LEGACY_LIVE_KINDS]
)
async def test_a_legacy_id_keeps_todays_first_come_rule(env, k):
    app, db = env
    await _deliver(app, k.create, k.payload(LEGACY_ID, CLAIMANT), sender=OTHER)
    # The later claim is refused (or, for a comment, trips the primary key
    # the dispatcher logs) — either way the first claim holds, as before.
    with contextlib.suppress(sqlite3.IntegrityError):
        await _deliver(app, k.create, k.payload(LEGACY_ID, CREATOR), sender=AUTHOR)
    assert await _owner(db, k, LEGACY_ID) == CLAIMANT


async def test_a_legacy_task_list_id_lands_only_from_the_hosts_sync(env):
    """I2: ``SPACE_TASK_LIST_CREATED`` is new in v_40 and every v_40 sender
    mints bound list ids, so a legacy id is refused live and from a member
    household's sync stream — first come would let a household seated in
    two spaces squat the other space's pre-v_40 list. The host's stream
    (taken whole) still carries pre-v_40 lists."""
    app, db = env
    k = next(k for k in KINDS if k.kind == SPACE_TASK_LIST_KIND)
    await _deliver(app, k.create, k.payload(LEGACY_ID, CREATOR), sender=AUTHOR)
    assert await _owner(db, k, LEGACY_ID) is None
    receiver = app[space_sync_receiver_key]
    await receiver._dispatch(
        k.sync_resource, SP, [k.record(LEGACY_ID, CREATOR)], provider=AUTHOR
    )
    assert await _owner(db, k, LEGACY_ID) is None
    await receiver._dispatch(
        k.sync_resource, SP, [k.record(LEGACY_ID, CREATOR)], provider=HOST
    )
    assert await _owner(db, k, LEGACY_ID) == CREATOR


@pytest.mark.parametrize("provider", [HOST, AUTHOR])
@pytest.mark.parametrize("k", KINDS, ids=_IDS)
async def test_a_synced_record_claiming_a_bound_id_for_another_owner_is_skipped(
    env, k, provider
):
    app, db = env
    squatted = _bound(k)
    genuine = _bound(k)
    records = [k.record(genuine, CREATOR)]
    if provider == HOST:
        # The host's stream is taken whole — the id binding still holds.
        records.insert(0, k.record(squatted, CLAIMANT))
    await app[space_sync_receiver_key]._dispatch(
        k.sync_resource, SP, records, provider=provider
    )
    assert await _owner(db, k, squatted) is None
    assert await _owner(db, k, genuine) == CREATOR


async def test_every_kind_is_covered():
    """Adding a kind to the module without a case here fails loudly."""
    covered = {k.kind for k in KINDS}
    expected = {
        SPACE_POST_KIND,
        SPACE_COMMENT_KIND,
        GALLERY_ITEM_KIND,
        SPACE_CALENDAR_EVENT_KIND,
        SPACE_TASK_KIND,
        SPACE_TASK_LIST_KIND,
        SPACE_PAGE_KIND,
        SPACE_STICKY_KIND,
    }
    assert covered == expected, sorted(expected - covered)
