"""Release-blocker protocol tests: space timetables (v_39).

Marked ``@pytest.mark.security``.

A space timetable is space content that only the space's owners / admins
may change. Against the real application (registry, repos, SQLite), these
tests prove:

* **Delivery** — a local edit fans out only to the space's member
  households (never a paired non-member, never a member below v_39), with
  the whole timetable inside the sealed payload: the envelope's plaintext
  is routing only.
* **Authority** — an inbound write lands only when the sending household
  moderates the space AND the user it is recorded as is a moderator seated
  on that household: a plain member household, a follower, a banned
  admin, a spoofed editor and a stranger are all refused.
* **Integrity** — an id of another space, an id not bound to its creator,
  an older or replayed version, and anything after a delete change
  nothing.
* **Robustness** — a hostile payload (huge, malformed, wrong schema,
  assignees) is dropped with a WARNING; the handler never raises.

The general matrices (``test_space_content_scope.py`` /
``test_space_content_authorship.py``) cover the event types too; this
file holds the timetable-specific attacks.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone
from types import MappingProxyType

import orjson
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    event_bus_key,
    federation_service_key,
    space_sync_receiver_key,
    space_timetable_service_key,
)
from socialhome.config import Config
from socialhome.domain.events import TimetableDeleted, TimetableSaved
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import SpacePermissionError
from socialhome.domain.timetable import (
    MAX_WIRE_BYTES,
    TimetableEntry,
    entry_to_dict,
)
from socialhome.federation.owner_bound_id import (
    SPACE_TIMETABLE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.federation_service import DeliveryResult

from .test_space_content_scope import timetable_wire

pytestmark = pytest.mark.security

FET = FederationEventType

SP = "sp-class"  # hosted by HOST; the space every attack targets
SP2 = "sp-other"  # another space HOST hosts
HOST = "the-host"
ADMIN = "house-admin"  # u-adm (admin), u-kid (member), u-bad (admin, banned)
MEMBER = "house-member"  # u-m (member)
FOLLOW = "house-follow"  # u-f (follower)
STRANGER = "house-stranger"  # no seat anywhere
LOCAL_USER = "u-local"

_TS = "2026-06-01T10:00:00.000000+00:00"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "timetables.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


def _bound(owner: str, space: str = SP) -> str:
    return mint_owner_bound_id(
        SPACE_TIMETABLE_KIND, space_id=space, owner_user_id=owner
    )


TT = _bound("u-adm")  # held here, in SP, at version 1
TT_OTHER = _bound("u-adm", SP2)  # held here, in SP2

_SEED = [
    (
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", LOCAL_USER, "Local"),
    ),
    *[
        (
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, feature_timetable) VALUES(?,?,?,?,?,1)",
            (sid, sid, HOST, "anna", "00" * 32),
        )
        for sid in (SP, SP2)
    ],
    *[
        (
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (sid, inst, uid, role),
        )
        for sid, inst, uid, role in (
            (SP, HOST, "u-h", "member"),
            (SP, ADMIN, "u-adm", "admin"),
            (SP, ADMIN, "u-kid", "member"),
            (SP, ADMIN, "u-bad", "admin"),
            (SP, MEMBER, "u-m", "member"),
            (SP, FOLLOW, "u-f", "subscriber"),
            (SP2, ADMIN, "u-adm", "admin"),
            # The space owner "anna", seated on the host. The roster wire
            # mirrors an owner as a plain ``member`` seat, so the host's own
            # live writer seats are what an owner's edit is judged on.
            (SP, HOST, "u-anna", "member"),
            (SP, HOST, "u-hf", "subscriber"),  # a follower on the host
        )
    ],
    # Removed (not banned) seats: an admin elsewhere and a host member.
    *[
        (
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
            " tombstoned) VALUES(?,?,?,?,1)",
            (SP, inst, uid, role),
        )
        for inst, uid, role in ((ADMIN, "u-gone", "admin"), (HOST, "u-hgone", "member"))
    ],
    (
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?,?,?)",
        (SP, "u-bad", "u-h"),
    ),
    *[
        (
            "INSERT INTO space_timetables(id, space_id, name, created_by,"
            " updated_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (tid, sid, "Plan", "u-adm", "u-adm", _TS, _TS),
        )
        for tid, sid in ((TT, SP), (TT_OTHER, SP2))
    ],
]


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    published: list = []

    async def _record(event) -> None:
        published.append(event)

    app[event_bus_key].subscribe(TimetableSaved, _record)
    app[event_bus_key].subscribe(TimetableDeleted, _record)
    return app, db, published


async def _rows(db) -> list[tuple]:
    rows = await db.fetchall("SELECT * FROM space_timetables ORDER BY id", ())
    return [tuple(r) for r in rows]


def _event(event_type, payload, *, sender, space_id=SP) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}-{sender}",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


async def _deliver(app, event_type, payload, *, sender, space_id=SP) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers, f"no handler for {event_type.value}"
    for handler in handlers:
        await handler(_event(event_type, payload, sender=sender, space_id=space_id))


def _upsert(tt_id=TT, *, by="u-adm", created_by="u-adm", version=2, **kw) -> dict:
    return {
        "timetable": timetable_wire(
            tt_id, created_by=created_by, updated_by=by, version=version, **kw
        )
    }


def _delete(tt_id=TT, *, by="u-adm", created_by="u-adm") -> dict:
    return {
        "timetable_id": tt_id,
        "created_by": created_by,
        "deleted_by": by,
        "deleted_at": _TS,
    }


async def _version(db, tt_id=TT) -> int | None:
    row = await db.fetchone(
        "SELECT version FROM space_timetables WHERE id=? AND deleted_at IS NULL",
        (tt_id,),
    )
    return None if row is None else int(row["version"])


# ── Positive controls ────────────────────────────────────────────────


async def test_an_admins_edit_lands_and_reaches_the_realtime_layer(env):
    app, db, published = env
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(), sender=ADMIN)
    assert await _version(db) == 2
    [ev] = published
    assert isinstance(ev, TimetableSaved)
    assert (ev.space_id, ev.origin_instance_id) == (SP, ADMIN)


async def test_an_admins_delete_tombstones_and_is_published(env):
    app, db, published = env
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(), sender=ADMIN)
    assert await _version(db) is None
    [ev] = published
    assert isinstance(ev, TimetableDeleted)
    assert (ev.timetable_id, ev.origin_instance_id, ev.deleted_by) == (
        TT,
        ADMIN,
        "u-adm",
    )


# ── Authority ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sender", "by"),
    [
        pytest.param(MEMBER, "u-m", id="a non-moderator member household"),
        pytest.param(FOLLOW, "u-f", id="a follower household"),
        pytest.param(ADMIN, "u-bad", id="a banned admin"),
        pytest.param(ADMIN, "u-kid", id="an admin household naming its member"),
        pytest.param(MEMBER, "u-adm", id="updated_by spoofed as another's admin"),
        pytest.param(ADMIN, LOCAL_USER, id="updated_by spoofed as our local user"),
        pytest.param(ADMIN, "", id="no editor at all"),
        pytest.param(STRANGER, "u-adm", id="a household with no seat"),
        pytest.param(ADMIN, "u-gone", id="a removed (not banned) admin"),
        pytest.param(HOST, "u-hf", id="the host naming its follower"),
        pytest.param(HOST, "u-hgone", id="the host naming its removed member"),
        pytest.param(HOST, "u-m", id="the host relaying a remote plain member"),
        pytest.param(HOST, "u-f", id="the host relaying a follower"),
        pytest.param(HOST, "u-gone", id="the host relaying a removed admin"),
        pytest.param(HOST, "u-bad", id="the host relaying a banned admin"),
    ],
)
async def test_only_a_moderator_seated_on_the_sender_may_write(env, sender, by):
    app, db, published = env
    before = await _rows(db)
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(by=by), sender=sender)
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(by=by), sender=sender)
    new_id = _bound("u-adm")
    await _deliver(
        app,
        FET.SPACE_TIMETABLE_UPSERTED,
        _upsert(new_id, by=by, version=1),
        sender=sender,
    )
    assert await _rows(db) == before
    assert published == []


@pytest.mark.parametrize(
    "by",
    [
        pytest.param("u-anna", id="the host's space owner"),
        pytest.param("u-h", id="a plain member seated on the host"),
        pytest.param("u-adm", id="a remote admin relayed by the host"),
    ],
)
async def test_the_host_records_its_owner_or_relays_a_live_admin(env, by):
    app, db, published = env
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(by=by), sender=HOST)
    assert await _version(db) == 2
    assert len(published) == 1


async def test_a_mesh_only_member_household_takes_the_owners_live_edit(env):
    """The class owner is the main editor. A member household that reached
    the space over the mesh or an invite link holds no ``remote_users`` row
    for the owner and no confirmed pairing with the host — the owner's live
    edit must still land, not wait for a sync that may never come."""
    app, db, published = env
    assert await db.fetchone("SELECT 1 FROM remote_users", ()) is None
    assert (
        await db.fetchone("SELECT 1 FROM remote_instances WHERE id=?", (HOST,)) is None
    )
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(by="u-anna"), sender=HOST)
    assert await _version(db) == 2
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(by="u-anna"), sender=HOST)
    assert await _version(db) is None
    assert len(published) == 2


# ── Integrity ────────────────────────────────────────────────────────


async def test_an_id_of_another_space_is_refused(env):
    app, db, published = env
    before = await _rows(db)
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(TT_OTHER, version=5), sender=ADMIN
    )
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(TT_OTHER), sender=ADMIN)
    # …and from the host, whose authority does not cross spaces either.
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(TT_OTHER, version=5), sender=HOST
    )
    assert await _rows(db) == before
    assert published == []


async def test_a_new_id_must_be_bound_to_its_creator_in_this_space(env):
    app, db, published = env
    before = await _rows(db)
    for tt_id in (
        _bound("u-kid"),  # bound to somebody else
        _bound("u-adm", SP2),  # bound to the creator, but in another space
        "0123456789abcdef0123456789abcdef",  # never bound
    ):
        await _deliver(
            app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(tt_id, version=1), sender=ADMIN
        )
    assert await _rows(db) == before
    assert published == []


async def test_a_replayed_or_older_version_changes_nothing(env):
    app, db, published = env
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=3, name="v3"), sender=ADMIN
    )
    after_v3 = await _rows(db)
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=3, name="v3"), sender=ADMIN
    )
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=2, name="v2"), sender=ADMIN
    )
    assert await _rows(db) == after_v3
    assert len(published) == 1


async def test_nothing_resurrects_a_deleted_timetable(env):
    app, db, published = env
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(), sender=ADMIN)
    after = await _rows(db)
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=99), sender=ADMIN)
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=99), sender=HOST)
    await app[space_sync_receiver_key]._dispatch(
        "timetables", SP, [_upsert(version=99)["timetable"]], provider=HOST
    )
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(), sender=ADMIN)
    assert await _rows(db) == after
    assert len(published) == 1  # the one real delete


async def test_a_delete_cannot_squat_another_spaces_timetable_id(env):
    """I1: a moderator of SP2 must not pre-tombstone (under SP2) an id that
    commits to SP — that would silently eat SP's real timetable forever."""
    app, db, published = env
    victim = _bound("u-adm", SP)
    await _deliver(
        app, FET.SPACE_TIMETABLE_DELETED, _delete(victim), sender=ADMIN, space_id=SP2
    )
    assert (
        await db.fetchone("SELECT 1 FROM space_timetables WHERE id=?", (victim,))
        is None
    )
    assert published == []
    await _deliver(
        app,
        FET.SPACE_TIMETABLE_UPSERTED,
        _upsert(victim, version=1),
        sender=HOST,
        space_id=SP,
    )
    assert await _version(db, victim) == 1


@pytest.mark.parametrize(
    ("tt_id", "created_by"),
    [
        pytest.param("junk-1", "u-adm", id="an unbound id"),
        pytest.param(_bound("u-kid"), "u-adm", id="bound to somebody else"),
        pytest.param(_bound("u-adm", SP2), "u-adm", id="bound to another space"),
        pytest.param(_bound("u-adm"), "", id="no creator named"),
        pytest.param(_bound("u-adm"), ["u-adm"], id="creator not a string"),
    ],
)
async def test_an_unseen_delete_creates_no_stub_unless_bound(
    env, tt_id, created_by, caplog
):
    """I1 / M3: an id this household never held is tombstoned only when it
    commits to its named creator in this space — junk leaves no rows."""
    app, db, published = env
    before = await _rows(db)
    with caplog.at_level(logging.WARNING):
        await _deliver(
            app,
            FET.SPACE_TIMETABLE_DELETED,
            _delete(tt_id, created_by=created_by),
            sender=ADMIN,
        )
    assert await _rows(db) == before
    assert published == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_junk_deletes_never_grow_the_table(env):
    app, db, _published = env
    for i in range(50):
        await _deliver(
            app, FET.SPACE_TIMETABLE_DELETED, _delete(f"junk-{i}"), sender=ADMIN
        )
    assert await db.fetchval("SELECT COUNT(*) FROM space_timetables", (), 0) == 2


async def test_a_legit_unseen_delete_still_tombstones(env):
    app, db, published = env
    unseen = _bound("u-adm")
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(unseen), sender=ADMIN)
    row = await db.fetchone(
        "SELECT space_id, deleted_at FROM space_timetables WHERE id=?", (unseen,)
    )
    assert row is not None and row["space_id"] == SP and row["deleted_at"]
    [ev] = published
    assert isinstance(ev, TimetableDeleted) and ev.created_by == "u-adm"


@pytest.mark.parametrize(
    "field", ["created_at", "updated_at"], ids=["created_at", "updated_at"]
)
@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+14:00",  # OverflowError on astimezone(UTC)
        "9999-12-31T23:59:59-14:00",
        "1969-12-31T23:59:59+00:00",
        "2200-01-01T00:00:00+00:00",
    ],
)
async def test_an_out_of_range_timestamp_is_dropped_not_raised(env, field, value):
    """M1: a timestamp that overflows or sits outside 1970–2199 is a
    validation failure — never an exception out of the handler or sync."""
    app, db, published = env
    before = await _rows(db)
    bad = _upsert()
    bad["timetable"][field] = value
    await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, bad, sender=ADMIN)
    good = _upsert(_bound("u-adm"), version=1)["timetable"]
    await app[space_sync_receiver_key]._dispatch(
        "timetables", SP, [bad["timetable"], good], provider=HOST
    )
    assert await _version(db, good["id"]) == 1  # the rest of the chunk applied
    after = [r for r in await _rows(db) if r[0] != good["id"]]
    assert after == before
    assert len(published) == 1


@pytest.mark.parametrize(
    "value",
    ["0001-01-01T00:00:00+14:00", "1969-01-01T00:00:00+00:00", "nope", None, 5],
)
async def test_an_unusable_deleted_at_is_dropped_with_a_warning(env, value, caplog):
    app, db, published = env
    before = await _rows(db)
    payload = _delete()
    payload["deleted_at"] = value
    with caplog.at_level(logging.WARNING):
        await _deliver(app, FET.SPACE_TIMETABLE_DELETED, payload, sender=ADMIN)
    assert await _rows(db) == before
    assert published == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    ("tt_id", "version"),
    [
        pytest.param(TT, 1 + 10_001, id="a jump of more than 10 000 versions"),
        pytest.param(TT, 2**31 - 1, id="the maximum version"),
        pytest.param(_bound("u-adm"), 2**31 - 1 - 10_000, id="a new row near the cap"),
    ],
)
async def test_version_inflation_is_refused(env, tt_id, version, caplog):
    """M2: a hostile moderator must not freeze a timetable by jumping its
    version out of every later editor's reach."""
    app, db, published = env
    before = await _rows(db)
    with caplog.at_level(logging.WARNING):
        await _deliver(
            app,
            FET.SPACE_TIMETABLE_UPSERTED,
            _upsert(tt_id, version=version),
            sender=ADMIN,
        )
        await app[space_sync_receiver_key]._dispatch(
            "timetables",
            SP,
            [_upsert(tt_id, version=version)["timetable"]],
            provider=HOST,
        )
    assert await _rows(db) == before
    assert published == []
    assert any("version" in r.getMessage() for r in caplog.records)
    # A normal step still lands.
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(version=1 + 10_000), sender=ADMIN
    )
    assert await _version(db) == 1 + 10_000


async def test_a_delete_that_overtakes_its_create_keeps_it_deleted(env):
    app, db, _published = env
    late = _bound("u-adm")
    await _deliver(app, FET.SPACE_TIMETABLE_DELETED, _delete(late), sender=ADMIN)
    await _deliver(
        app, FET.SPACE_TIMETABLE_UPSERTED, _upsert(late, version=1), sender=ADMIN
    )
    assert await _version(db, late) is None


# ── Robustness ───────────────────────────────────────────────────────


def _huge() -> dict:
    entries = [
        TimetableEntry(
            id=f"e{wd}-{i}",
            weekday=wd,
            start=time(i // 2, (i % 2) * 30),
            end=time(i // 2, (i % 2) * 30 + 29),
            title="ü" * 60,
            room="ü" * 30,
            teacher="ü" * 60,
            note="ü" * 200,
        )
        for wd in range(7)
        for i in range(24)
    ][:200]
    wire = timetable_wire(
        TT, created_by="u-adm", updated_by="u-adm", days=tuple(range(7))
    )
    wire["entries"] = [entry_to_dict(e) for e in entries]
    assert len(orjson.dumps(wire)) > MAX_WIRE_BYTES
    return {"timetable": wire}


def _patched(**fields) -> dict:
    wire = _upsert()["timetable"]
    wire.update(fields)
    return {"timetable": wire}


_HOSTILE = [
    pytest.param({}, id="no timetable"),
    pytest.param({"timetable": ["not", "a", "dict"]}, id="not an object"),
    pytest.param({"timetable": "x" * 100_000}, id="a string"),
    pytest.param(_patched(schema=2), id="wrong schema"),
    pytest.param(_patched(schema="1"), id="schema as a string"),
    pytest.param(_patched(version="2"), id="version as a string"),
    pytest.param(_patched(version=2**40), id="version out of range"),
    pytest.param(_patched(id="x" * 100), id="id too long"),
    pytest.param(_patched(name="  padded  "), id="untrimmed name"),
    pytest.param(_patched(tz="../../etc/passwd"), id="hostile tz"),
    pytest.param(_patched(days=[9]), id="bad weekday"),
    pytest.param(_patched(entries=[{"id": 1}]), id="malformed entry"),
    pytest.param(_patched(entries=[{}] * 10_000), id="too many entries"),
    pytest.param(_patched(assignees=["u-adm"]), id="assignees set"),
    pytest.param(_huge(), id="over the 128 KiB wire cap"),
]


@pytest.mark.parametrize("payload", _HOSTILE)
async def test_a_hostile_upsert_is_dropped_with_a_warning(env, payload, caplog):
    app, db, published = env
    before = await _rows(db)
    with caplog.at_level(logging.WARNING):
        await _deliver(app, FET.SPACE_TIMETABLE_UPSERTED, payload, sender=ADMIN)
        await app[space_sync_receiver_key]._dispatch(
            "timetables", SP, [payload.get("timetable")], provider=HOST
        )
    assert await _rows(db) == before
    assert published == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no id"),
        pytest.param({"timetable_id": 7, "deleted_by": "u-adm"}, id="id not a string"),
        pytest.param(
            {"timetable_id": "x" * 10_000, "deleted_by": "u-adm"}, id="huge id"
        ),
        pytest.param({"timetable_id": TT, "deleted_by": ["u-adm"]}, id="editor a list"),
    ],
)
async def test_a_hostile_delete_is_dropped_with_a_warning(env, payload, caplog):
    app, db, published = env
    before = await _rows(db)
    with caplog.at_level(logging.WARNING):
        await _deliver(app, FET.SPACE_TIMETABLE_DELETED, payload, sender=ADMIN)
    assert await _rows(db) == before
    assert published == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ── Delivery: members only, sealed ───────────────────────────────────


MEMBER_NEW = "peer-member-v39"
MEMBER_OLD = "peer-member-v38"
PAIRED_NON_MEMBER = "peer-not-a-member"
LOCAL_SPACE = "sp-local"


@pytest.fixture
async def local_space(env, monkeypatch):
    """A space THIS household hosts, one local admin, two member households
    (one below v_39) and a paired household that is not a member."""
    app, db, _published = env
    fed = app[federation_service_key]
    session_key = os.urandom(32)
    wrapped = fed._key_manager.encrypt(session_key)
    for iid, version in (
        (MEMBER_NEW, FederationCapability.MIN_FOR_SPACE_TIMETABLE),
        (MEMBER_OLD, FederationCapability.MIN_FOR_SPACE_TIMETABLE - 1),
        (PAIRED_NON_MEMBER, FederationCapability.MIN_FOR_SPACE_TIMETABLE),
    ):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source, proto_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                iid,
                iid,
                "00" * 32,
                wrapped,
                wrapped,
                f"https://{iid}/wh",
                f"wh-{iid}",
                "confirmed",
                "manual",
                version,
            ),
        )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_timetable) VALUES(?,?,?,?,?,1)",
        (LOCAL_SPACE, "Class", fed._own_instance_id, "local", "00" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'owner')",
        (LOCAL_SPACE, LOCAL_USER),
    )
    for iid in (fed._own_instance_id, MEMBER_NEW, MEMBER_OLD):
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (LOCAL_SPACE, iid),
        )
    sent: list[dict] = []

    async def _capture(_self, *, to_instance_id, event_type, payload, space_id=None):
        sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    monkeypatch.setattr(type(fed), "send_with_mesh_fallback", _capture)
    return app, sent, session_key


async def test_edits_reach_member_households_only(local_space):
    app, sent, _key = local_space
    scope = app[space_timetable_service_key].scope(LOCAL_SPACE, LOCAL_USER)
    tt = await scope.create(name="Klasse 5b", created_by=LOCAL_USER)
    await scope.update(tt.id, version=1, by=LOCAL_USER, name="Klasse 5c")
    await scope.delete(tt.id, by=LOCAL_USER)
    assert [(s["to"], s["event_type"]) for s in sent] == [
        (MEMBER_NEW, FET.SPACE_TIMETABLE_UPSERTED),
        (MEMBER_NEW, FET.SPACE_TIMETABLE_UPSERTED),
        (MEMBER_NEW, FET.SPACE_TIMETABLE_DELETED),
    ]
    assert all(s["space_id"] == LOCAL_SPACE for s in sent)
    assert sent[1]["payload"]["timetable"]["name"] == "Klasse 5c"
    assert sent[2]["payload"]["timetable_id"] == tt.id


async def test_the_timetable_travels_only_inside_the_sealed_payload(local_space):
    """Everything but the routing fields is encrypted: the envelope's
    plaintext names no timetable, lesson, room or editor."""
    app, sent, session_key = local_space
    fed = app[federation_service_key]
    scope = app[space_timetable_service_key].scope(LOCAL_SPACE, LOCAL_USER)
    tt = await scope.create(name="Geheimplan", created_by=LOCAL_USER, template="empty")
    await scope.add_entry(
        tt.id,
        version=1,
        by=LOCAL_USER,
        fields={
            "weekday": 0,
            "start": "08:00",
            "end": "08:45",
            "title": "Mathematik",
            "room": "Raum-B2",
            "teacher": "Frau Keller",
        },
    )
    instance = await fed._federation_repo.get_instance(MEMBER_NEW)
    for s in sent:
        envelope = fed._seal_envelope(
            instance,
            event_type=s["event_type"],
            payload=s["payload"],
            space_id=s["space_id"],
        )
        assert set(envelope) - {"encrypted_payload", "signatures"} == {
            "msg_id",
            "event_type",
            "from_instance",
            "to_instance",
            "timestamp",
            "space_id",
            "proto_version",
            "sig_suite",
        }
        clear = orjson.dumps({k: v for k, v in envelope.items()}).decode()
        for secret in ("Geheimplan", "Mathematik", "Raum-B2", "Keller", LOCAL_USER):
            assert secret not in clear
        opened = orjson.loads(
            fed._encoder.decrypt_payload(envelope["encrypted_payload"], session_key)
        )
        assert opened == s["payload"]
        assert opened["timetable"]["updated_by"] == LOCAL_USER
    assert sent[-1]["payload"]["timetable"]["entries"][0]["title"] == "Mathematik"


async def test_a_follower_or_member_cannot_make_the_household_broadcast(local_space):
    """The only way onto the wire is the admin-gated service."""
    app, sent, _key = local_space
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("kid", "u-local-kid", "Kid"),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'member')",
        (LOCAL_SPACE, "u-local-kid"),
    )
    scope = app[space_timetable_service_key].scope(LOCAL_SPACE, "u-local-kid")
    with pytest.raises(SpacePermissionError):
        await scope.create(name="Mine", created_by="u-local-kid")
    assert sent == []
