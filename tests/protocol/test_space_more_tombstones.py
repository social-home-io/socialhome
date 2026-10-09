"""Release-blocker protocol tests: a missed delete of a space sticky,
calendar event, gallery album / item or zone heals by §25.6 sync, and a
deleted post takes its poll and schedule with it (migration 0085).

Marked ``@pytest.mark.security``.

Three real households, each a full app over its own SQLite file:

* **H** — holds the host's data; its streams are dispatched as the
  space's host (``HOST``);
* **C** — a member household (``MEMBER``, its user ``u-cora``) that was
  offline while H deleted a row;
* **D** — a household joining the space later, which catches up from H
  and from C.

Three more households exist only as seats: ``OTHER`` (a plain member),
``MOD`` (a moderator household) and ``ADMIN`` (an admin household).

For each type:

* a row deleted while C was offline is deleted on C by H's ``*_deleted``
  stream — content gone, the id a tombstone; C, once caught up, never
  streams it to D again;
* H's tombstone of an id D never held leaves a stub, so C's stale copy
  streamed afterwards cannot create it (an owner-bound id only — zone ids
  are not, so a zone is never stubbed);
* a forged tombstone — from a household the live delete rule refuses — is
  refused; a tombstone lands in a space archived here.

And a post deleted while C was offline takes its poll / schedule rows on
C, and a schedule streamed for it afterwards does not bring them back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    space_sync_receiver_key,
    space_sync_service_key,
)
from socialhome.config import Config
from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import SpaceZone
from socialhome.domain.sticky import Sticky
from socialhome.federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import REMOVAL_RESOURCES, RESOURCE_ORDER

pytestmark = pytest.mark.security

SPACE = "sp-more"
HOST = "the-host"
MEMBER = "the-member"
OTHER = "other-member"
MOD = "mod-house"
ADMIN = "admin-house"
STRANGER = "no-seat-house"
ANNA = "u-anna"  # the host's user
CORA = "u-cora"  # C's user
OLIVER = "u-oliver"  # a plain member on OTHER
MOLLY = "u-molly"  # a moderator on MOD
ADAM = "u-adam"  # an admin on ADMIN
SEATS = (
    (HOST, ANNA, "member"),
    (MEMBER, CORA, "member"),
    (OTHER, OLIVER, "member"),
    (MOD, MOLLY, "moderator"),
    (ADMIN, ADAM, "admin"),
)
_NOW = datetime.now(timezone.utc)


def _config(tmp_path, name: str) -> Config:
    d = tmp_path / name
    d.mkdir()
    return Config(
        data_dir=str(d),
        db_path=str(d / "sh.db"),
        media_path=str(d / "media"),
        apps_path=str(d / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=1,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


async def _seed(db, *, local: str | None = None) -> None:
    """The space hosted by ``HOST``, every household seated; ``local`` is
    the one user who lives on this household."""
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (SPACE, SPACE, HOST, "anna", "00" * 32),
    )
    for instance, user, role in SEATS:
        if user == local:
            await db.enqueue(
                "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
                (user, user, user),
            )
            await db.enqueue(
                "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
                (SPACE, user, role),
            )
            continue
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (SPACE, instance),
        )
        await db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (SPACE, instance, user, role),
        )
        # Every peer advertises v_42+: it names the actor of each write.
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
            " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 42, ?)",
            (
                instance,
                instance,
                "ab" * 32,
                f"https://{instance}/inbox/x",
                f"{instance}_local",
                _NOW.isoformat(),
            ),
        )


@pytest.fixture
async def houses(aiohttp_client, tmp_path):
    h = create_app(_config(tmp_path, "h"))
    c = create_app(_config(tmp_path, "c"))
    d = create_app(_config(tmp_path, "d"))
    for app in (h, c, d):
        await aiohttp_client(app)
    await _seed(h[db_key])
    await _seed(c[db_key], local=CORA)
    await _seed(d[db_key])
    return h, c, d


def _rx(app):
    return app[space_sync_receiver_key]


async def _sync(provider_app, to_app, *, provider: str, only=None) -> None:
    """Stream every resource of ``provider_app``'s space to ``to_app``'s
    receiver as ``provider``, in wire order (chunking + crypto aside)."""
    exporters = provider_app[space_sync_service_key]._exporters
    receiver = _rx(to_app)
    for resource in RESOURCE_ORDER:
        if only is not None and resource not in only:
            continue
        exporter = exporters.get(resource)
        if exporter is None:
            continue
        records = await exporter.list_records(SPACE)
        await receiver._dispatch(resource, SPACE, records, provider=provider)


async def _dispatch(app, resource: str, records: list[dict], *, provider: str):
    await _rx(app)._dispatch(resource, SPACE, records, provider=provider)


# ── One type, as the tests see it ───────────────────────────────────────


@dataclass(frozen=True)
class Kind:
    """A tombstoned type: its resources, how to hold a row of C's user on
    some households (returns the id), how the host deletes it, the table
    and the column whose content a tombstone must not keep."""

    name: str
    tomb: str
    live: str
    table: str
    content_col: str
    hold: Callable[[tuple, str], Awaitable[None]]
    new_id: Callable[[], str]
    delete: Callable[[object, str], Awaitable[bool]]
    #: Who the live delete rule refuses (with the record it would send).
    forger: str
    #: Who the live rule admits from a member household.
    rightful: str
    #: An owner-bound id kind — the host stubs ids never held here.
    stubs: bool = True


async def _hold_sticky(apps, sid):
    for app in apps:
        assert await _rx(app)._sticky_repo.save(
            Sticky(
                id=sid,
                author=CORA,
                content="cora's secret note",
                color="#FFF9B1",
                position_x=1.0,
                position_y=1.0,
                created_at=_NOW.isoformat(),
                updated_at=_NOW.isoformat(),
                space_id=SPACE,
            ),
            space_id=SPACE,
        )


async def _hold_event(apps, eid):
    for app in apps:
        assert await _rx(app)._space_calendar_repo.save_event(
            CalendarEvent(
                id=eid,
                calendar_id=SPACE,
                summary="cora's secret party",
                start=_NOW,
                end=_NOW,
                created_by=CORA,
            ),
            space_id=SPACE,
        )


def _album_of(item_id: str) -> str:
    return f"al-{item_id[-8:]}"


async def _hold_album_with_item(apps, album_id, item_id=None):
    for app in apps:
        repo = _rx(app)._gallery_repo
        await repo.create_album(
            GalleryAlbum(
                id=album_id,
                space_id=SPACE,
                owner_user_id=CORA,
                name="cora's secret trip",
                item_count=1 if item_id else 0,
            )
        )
        if item_id:
            await repo.create_item(
                GalleryItem(
                    id=item_id,
                    album_id=album_id,
                    uploaded_by=CORA,
                    item_type="photo",
                    url=f"api/media/{item_id}.webp",
                    thumbnail_url=f"api/media/{item_id}-t.webp",
                    width=1,
                    height=1,
                    caption="cora's secret beach",
                )
            )


#: Each gallery-item test file its item under a bound album of C's user.
_ITEM_ALBUMS: dict[str, str] = {}


def _new_item_id() -> str:
    item_id = mint_owner_bound_id(GALLERY_ITEM_KIND, space_id=SPACE, owner_user_id=CORA)
    _ITEM_ALBUMS[item_id] = mint_owner_bound_id(
        GALLERY_ALBUM_KIND, space_id=SPACE, owner_user_id=CORA
    )
    return item_id


async def _hold_item(apps, item_id):
    await _hold_album_with_item(apps, _ITEM_ALBUMS[item_id], item_id)


async def _hold_album(apps, album_id):
    await _hold_album_with_item(apps, album_id, f"it-of-{album_id}")


async def _hold_zone(apps, zid):
    for app in apps:
        assert await _rx(app)._zone_repo.upsert(
            SpaceZone(
                id=zid,
                space_id=SPACE,
                name="Cora's secret place",
                latitude=47.1234,
                longitude=8.5678,
                radius_m=100,
                color=None,
                created_by=ADAM,
                created_at=_NOW.isoformat(),
                updated_at=_NOW.isoformat(),
            ),
            space_id=SPACE,
        )


_seq = iter(range(10**6))


KINDS = [
    Kind(
        name="sticky",
        tomb="stickies_deleted",
        live="stickies",
        table="stickies",
        content_col="content",
        hold=_hold_sticky,
        new_id=lambda: mint_owner_bound_id(
            SPACE_STICKY_KIND, space_id=SPACE, owner_user_id=CORA
        ),
        delete=lambda h, sid: _rx(h)._sticky_repo.delete(
            sid, space_id=SPACE, deleted_by=ANNA
        ),
        forger=STRANGER,
        rightful=OTHER,
    ),
    Kind(
        name="calendar event",
        tomb="calendar_deleted",
        live="calendar",
        table="space_calendar_events",
        content_col="summary",
        hold=_hold_event,
        new_id=lambda: mint_owner_bound_id(
            SPACE_CALENDAR_EVENT_KIND, space_id=SPACE, owner_user_id=CORA
        ),
        delete=lambda h, eid: _rx(h)._space_calendar_repo.delete_event(
            eid, space_id=SPACE, deleted_by=ANNA
        ),
        forger=STRANGER,
        rightful=OTHER,
    ),
    Kind(
        name="gallery item",
        tomb="gallery_items_deleted",
        live="gallery",
        table="gallery_items",
        content_col="caption",
        hold=_hold_item,
        new_id=_new_item_id,
        delete=lambda h, iid: _rx(h)._gallery_repo.delete_item_in_space(
            iid, space_id=SPACE, deleted_by=ANNA
        ),
        # Not the uploader's household, no content authority.
        forger=OTHER,
        rightful=MOD,
    ),
    Kind(
        name="gallery album",
        tomb="gallery_albums_deleted",
        live="gallery",
        table="gallery_albums",
        content_col="name",
        hold=_hold_album,
        new_id=lambda: mint_owner_bound_id(
            GALLERY_ALBUM_KIND, space_id=SPACE, owner_user_id=CORA
        ),
        delete=lambda h, aid: _rx(h)._gallery_repo.delete_album_in_space(
            aid, space_id=SPACE, deleted_by=ANNA
        ),
        # A whole album is settings authority: a moderator seat is not enough.
        forger=MOD,
        rightful=ADMIN,
    ),
    Kind(
        name="zone",
        tomb="space_zones_deleted",
        live="space_zones",
        table="space_zones",
        content_col="name",
        hold=_hold_zone,
        new_id=lambda: f"z_test{next(_seq)}",
        delete=lambda h, zid: _rx(h)._zone_repo.delete(
            zid, space_id=SPACE, deleted_by=ADAM
        ),
        forger=MOD,
        rightful=ADMIN,
        stubs=False,
    ),
]
KIND_IDS = [k.name for k in KINDS]


async def _row(app, kind: Kind, row_id: str) -> dict | None:
    row = await app[db_key].fetchone(
        f"SELECT deleted_at, {kind.content_col} AS body FROM {kind.table} WHERE id=?",
        (row_id,),
    )
    return dict(row) if row is not None else None


async def _live(app, kind: Kind, row_id: str) -> bool:
    row = await _row(app, kind, row_id)
    return row is not None and row["deleted_at"] is None


async def _tombstoned(app, kind: Kind, row_id: str) -> bool:
    row = await _row(app, kind, row_id)
    return row is not None and row["deleted_at"] is not None


async def _scenario(h, c, kind: Kind) -> str:
    """A row of C's user held by H and C; H deletes it while C is offline."""
    row_id = kind.new_id()
    await kind.hold((h, c), row_id)
    assert await kind.delete(h, row_id)
    return row_id


# ── A missed delete heals ────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_a_delete_missed_while_offline_heals_on_catch_up(houses, kind):
    h, c, _d = houses
    row_id = await _scenario(h, c, kind)
    assert await _live(c, kind, row_id)

    await _sync(h, c, provider=HOST)

    row = await _row(c, kind, row_id)
    assert row is not None and row["deleted_at"] is not None
    assert "secret" not in str(row["body"] or "")
    # A second stream is a no-op.
    await _sync(h, c, provider=HOST)
    assert await _tombstoned(c, kind, row_id)


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_a_caught_up_household_never_re_exports_it(houses, kind):
    h, c, d = houses
    row_id = await _scenario(h, c, kind)
    await _sync(h, c, provider=HOST)
    exporters = c[space_sync_service_key]._exporters
    live = await exporters[kind.live].list_records(SPACE)
    assert row_id not in {r.get("id") for r in live}
    # C streams the tombstone on, identity only: never the content.
    tombs = await exporters[kind.tomb].list_records(SPACE)
    assert row_id in {r["id"] for r in tombs}
    assert "secret" not in repr(tombs) and "47.12" not in repr(tombs)
    await _sync(c, d, provider=MEMBER)
    assert not await _live(d, kind, row_id)


@pytest.mark.parametrize("kind", [k for k in KINDS if k.stubs], ids=lambda k: k.name)
async def test_a_hosts_tombstone_stubs_an_id_never_held(houses, kind):
    h, c, d = houses
    row_id = await _scenario(h, c, kind)
    # D never saw it: the host's tombstone leaves a content-free stub …
    # (A joiner's item stub needs the album, which the live ``gallery``
    # resource — ordered before the item tombstones — brings.)
    await _sync(h, d, provider=HOST, only={kind.tomb, "gallery"})
    assert await _tombstoned(d, kind, row_id)
    # … so C's stale stream of it afterwards cannot create it.
    await _sync(c, d, provider=MEMBER)
    assert not await _live(d, kind, row_id)


async def test_a_zone_never_held_is_not_stubbed_and_heals_on_the_host_stream(houses):
    """Zone ids are not owner-bound: nothing proves one is this space's, so
    the host's tombstone of a zone D never held records nothing. A stale
    admin household's copy then lands — and the host's next stream removes
    it."""
    h, c, d = houses
    zones = next(k for k in KINDS if k.name == "zone")
    row_id = await _scenario(h, c, zones)
    await _sync(h, d, provider=HOST, only={"space_zones_deleted"})
    assert await _row(d, zones, row_id) is None
    await _sync(c, d, provider=ADMIN, only={"space_zones"})
    assert await _live(d, zones, row_id)
    await _sync(h, d, provider=HOST)
    assert await _tombstoned(d, zones, row_id)
    await _sync(c, d, provider=ADMIN, only={"space_zones"})
    assert not await _live(d, zones, row_id)


@pytest.mark.parametrize("kind", [k for k in KINDS if k.stubs], ids=lambda k: k.name)
async def test_a_member_households_tombstone_never_stubs(houses, kind):
    _h, c, _d = houses
    row_id = kind.new_id()
    owner_key = {
        "sticky": "author",
        "calendar event": "created_by",
        "gallery item": "uploaded_by",
        "gallery album": "owner_user_id",
    }[kind.name]
    record = {"id": row_id, owner_key: CORA}
    if kind.name == "gallery item":
        record["album_id"] = _ITEM_ALBUMS[row_id]
    await _dispatch(c, kind.tomb, [record], provider=MEMBER)
    assert await _row(c, kind, row_id) is None


# ── Who may stream a delete ─────────────────────────────────────────────


def _record_for(kind: Kind, row_id: str) -> dict:
    owner_key = {
        "sticky": "author",
        "calendar event": "created_by",
        "gallery item": "uploaded_by",
        "gallery album": "owner_user_id",
        "zone": "created_by",
    }[kind.name]
    record = {"id": row_id, owner_key: ADAM if kind.name == "zone" else CORA}
    if kind.name == "gallery item":
        record["album_id"] = _ITEM_ALBUMS[row_id]
    return record


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_a_forged_tombstone_is_refused_and_a_rightful_one_lands(houses, kind):
    _h, c, _d = houses
    row_id = kind.new_id()
    await kind.hold((c,), row_id)
    record = _record_for(kind, row_id)
    for forger in (kind.forger, STRANGER, "nobody"):
        await _dispatch(c, kind.tomb, [record], provider=forger)
        assert await _live(c, kind, row_id), forger
    await _dispatch(c, kind.tomb, [record], provider=kind.rightful)
    assert await _tombstoned(c, kind, row_id)


async def test_a_restricted_level_refuses_a_plain_members_sticky_tombstone(houses):
    _h, c, _d = houses
    await c[db_key].enqueue(
        "UPDATE spaces SET stickies_access='admin_only' WHERE id=?", (SPACE,)
    )
    stickies = KINDS[0]
    row_id = stickies.new_id()
    await stickies.hold((c,), row_id)
    record = {"id": row_id, "author": CORA, "actor_user_id": OLIVER}
    await _dispatch(c, "stickies_deleted", [record], provider=OTHER)
    assert await _live(c, stickies, row_id)
    # An admin's delete, from the admin's household, passes the level.
    record["actor_user_id"] = ADAM
    await _dispatch(c, "stickies_deleted", [record], provider=ADMIN)
    assert await _tombstoned(c, stickies, row_id)


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_tombstones_land_in_a_space_archived_here(houses, kind):
    h, c, _d = houses
    row_id = await _scenario(h, c, kind)
    await c[db_key].enqueue(
        "UPDATE spaces SET archived=1, archived_reason='removed' WHERE id=?",
        (SPACE,),
    )
    late = kind.new_id()
    await kind.hold((h,), late)
    await _sync(h, c, provider=HOST)
    assert await _tombstoned(c, kind, row_id)  # the removal landed …
    assert not await _live(c, kind, late)  # … new content did not
    assert kind.tomb in REMOVAL_RESOURCES


# ── The deleted rows' files and dependants ──────────────────────────────


async def test_a_streamed_gallery_delete_unlinks_the_files(houses):
    h, c, _d = houses
    items = next(k for k in KINDS if k.name == "gallery item")
    albums = next(k for k in KINDS if k.name == "gallery album")
    item_id = await _scenario(h, c, items)
    album_id = albums.new_id()
    await albums.hold((h, c), album_id)
    assert await albums.delete(h, album_id)
    media = c[space_sync_receiver_key]._media_dir
    media.mkdir(parents=True, exist_ok=True)
    names = [
        f"{item_id}.webp",
        f"{item_id}-t.webp",
        f"it-of-{album_id}.webp",
        f"it-of-{album_id}-t.webp",
    ]
    for name in names:
        (media / name).write_bytes(b"x")
    await _sync(h, c, provider=HOST)
    assert not any((media / name).exists() for name in names)


async def test_a_calendar_tombstone_takes_the_rsvps(houses):
    h, c, _d = houses
    events = KINDS[1]
    row_id = await _scenario(h, c, events)
    await c[db_key].enqueue(
        "INSERT INTO space_calendar_rsvps(event_id, user_id, status, occurrence_at)"
        " VALUES(?, ?, 'going', ?)",
        (row_id, CORA, _NOW.isoformat()),
    )
    await _sync(h, c, provider=HOST)
    assert (
        await c[db_key].fetchone(
            "SELECT 1 FROM space_calendar_rsvps WHERE event_id=?", (row_id,)
        )
        is None
    )


async def test_a_post_tombstone_takes_its_poll_and_schedule(houses):
    h, c, _d = houses
    pid = mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=CORA)
    for app in (h, c):
        assert (
            await _rx(app)._space_post_repo.save(
                SPACE,
                Post(
                    id=pid,
                    author=CORA,
                    type=PostType.POLL,
                    created_at=_NOW,
                    content="Which day?",
                ),
            )
            is not None
        )
        polls = _rx(app)._poll_repo
        await polls.create_poll(
            post_id=pid,
            question="Which day?",
            closes_at=None,
            allow_multiple=False,
            options=[{"id": f"o-{pid[-6:]}", "text": "Mon"}],
        )
        await polls.insert_vote(option_id=f"o-{pid[-6:]}", voter_user_id=CORA)
        await polls.create_schedule_poll_in_space(
            space_id=SPACE,
            post_id=pid,
            title="When?",
            deadline=None,
            slots=[{"id": f"s-{pid[-6:]}", "slot_date": "2026-11-01"}],
        )
    assert await _rx(h)._space_post_repo.soft_delete(pid, space_id=SPACE)
    # Deleting the post here dropped its poll here …
    assert await _rx(h)._poll_repo.get_meta(pid) is None
    assert await _rx(c)._poll_repo.get_meta(pid) is not None
    # … and the tombstone C catches up on drops it there too.
    await _sync(h, c, provider=HOST, only={"posts_deleted"})
    polls = _rx(c)._poll_repo
    assert await polls.get_meta(pid) is None
    assert await polls.get_schedule_meta(pid) is None
    assert await polls.list_schedule_slots(pid) == []
    votes = await c[db_key].fetchone(
        "SELECT COUNT(*) AS n FROM space_poll_votes WHERE voter_user_id=?", (CORA,)
    )
    assert votes["n"] == 0
    # A schedule streamed for it afterwards (the host's, or a stale copy)
    # does not bring it back.
    await _dispatch(
        c,
        "schedules",
        [
            {
                "post_id": pid,
                "title": "Back?",
                "slots": [{"id": "s-new", "slot_date": "x"}],
            }
        ],
        provider=HOST,
    )
    assert await polls.get_schedule_meta(pid) is None
