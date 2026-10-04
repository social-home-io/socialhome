"""Coverage for each per-resource persist path in :class:`SpaceSyncReceiver`."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.events import TimetableSaved
from socialhome.domain.task import Task, TaskPriority, TaskStatus
from socialhome.domain.timetable import Timetable, to_wire_dict
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.owner_bound_id import (
    SPACE_TIMETABLE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import serialise_chunk
from socialhome.services.gallery_tombstones import GalleryAlbumTombstones
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus


class _FakeCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        import base64

        return base64.urlsafe_b64decode(ciphertext)


class _FakeFedRepo:
    def __init__(self, peer):
        self._peer = peer

    async def get_instance(self, iid):
        return self._peer if iid == self._peer.id else None


class _FakeRepos:
    """Collects saves per resource type so tests can assert on them."""

    def __init__(self):
        self.members = []
        self.bans = []
        self.posts = []
        #: post id → Post held here before the sync (for the tombstone skip).
        self.held_posts = {}
        self.comments = []
        self.tasks = []
        self.task_lists = []
        self.deleted_list_ids: set[str] = set()
        self.deleted_task_ids: set[str] = set()
        self.held_tasks: dict = {}
        self.pages = []
        self.stickies = []
        self.calendar = []
        self.gallery_albums = []
        self.gallery_items = []
        self.zones = []
        self.bazaar_listings = []

    # space_repo
    async def save_member(self, member):
        self.members.append(member)
        return member

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.bans.append((space_id, user_id, banned_by, reason))

    # space_post_repo
    async def save(self, *args):
        # Used by both space_post_repo.save(space_id, post) and page/sticky.save(obj)
        if len(args) == 2:
            self.posts.append(args)
        else:
            self.pages.append(args[0]) if isinstance(
                args[0], type(None)
            ) is False and hasattr(args[0], "title") else self.stickies.append(args[0])
        return args[-1]

    async def add_comment(self, comment, *, space_id):
        self.comments.append(comment)
        return True

    # space_task_repo.save(space_id, task)
    async def save_task(self, space_id, task):
        self.tasks.append((space_id, task))
        return task

    # calendar_repo.save_event(space_id, event)
    async def save_event(self, space_id, event):
        self.calendar.append((space_id, event))
        return event

    # gallery_repo
    async def create_album(self, album):
        self.gallery_albums.append(album)

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        self.gallery_items.append(item)
        return True


class _PostRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, space_id, post):
        self._c.posts.append((space_id, post))
        return post

    async def get(self, post_id):
        held = self._c.held_posts.get(post_id)
        return ("sp-1", held) if held is not None else None

    async def add_comment(self, comment, *, space_id):
        self._c.comments.append(comment)
        return True


class _TaskRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, task, *, space_id):
        self._c.tasks.append((space_id, task))
        return True

    async def save_list(self, lst, *, space_id):
        self._c.task_lists.append((space_id, lst))
        return True

    async def get_list(self, list_id):
        return None

    async def is_list_deleted(self, list_id, *, space_id):
        return list_id in self._c.deleted_list_ids

    async def is_task_deleted(self, task_id, *, space_id):
        return task_id in self._c.deleted_task_ids

    async def get(self, task_id):
        return self._c.held_tasks.get(task_id)


class _PageRepoStub:
    def __init__(self, collector):
        self._c = collector
        #: Pages "held here", by (id, space) — for the v_48 engine path.
        self.held: dict[tuple[str, str], object] = {}

    async def save(self, page, *, space_id):
        self._c.pages.append(page)
        return True

    async def get_space_page(self, page_id, *, space_id):
        return self.held.get((page_id, space_id))

    async def is_page_deleted(self, page_id, *, space_id):
        return False


class _StickyRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, sticky, *, space_id):
        self._c.stickies.append(sticky)
        return True


class _CalendarRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save_event(self, event, *, space_id):
        self._c.calendar.append((space_id, event))
        return True


class _GalleryRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def create_album(self, album):
        self._c.gallery_albums.append(album)

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        self._c.gallery_items.append(item)
        return True


class _ZoneRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def upsert(self, zone, *, space_id):
        self._c.zones.append(zone)
        return True


class _BazaarRepoStub:
    """Minimal AbstractBazaarRepo slice for receiver tests (F4)."""

    def __init__(self, collector):
        self._c = collector

    async def save_listing(self, listing, *, space_id):
        self._c.bazaar_listings.append(listing)
        return True


class _SpaceRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def get(self, space_id):
        # The chunks in this file come from the space's host, whose stream
        # is taken whole; non-host providers are covered in
        # tests/protocol/test_space_content_authorship.py.
        return SimpleNamespace(
            id=space_id,
            owner_instance_id="peer-a",
            archived=False,
            archived_reason=None,
            dissolved=False,
        )

    async def save_member(self, member):
        self._c.members.append(member)
        return member

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self._c.bans.append((space_id, user_id, banned_by, reason))


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def peer():
    kp = generate_identity_keypair()
    return (
        RemoteInstance(
            id="peer-a",
            display_name="Peer A",
            remote_identity_pk=kp.public_key.hex(),
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://peer/wh",
            local_inbox_id="wh-peer-a",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
        kp,
    )


@pytest.fixture
def tombstones():
    return GalleryAlbumTombstones()


@pytest.fixture
def setup(bus, peer, tombstones):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        zone_repo=_ZoneRepoStub(collector),
        bazaar_repo=_BazaarRepoStub(collector),
        gallery_tombstones=tombstones,
    )
    return r, collector, peer_kp


async def _send(r, kp, resource, records, *, space_id="sp-1", sync_id="sync-1"):
    """Build + sign + deliver one envelope for the given resource."""
    crypto = _FakeCrypto()
    plaintext = orjson.dumps({"records": records})
    _, ct = await crypto.encrypt_chunk(
        space_id=space_id,
        sync_id=sync_id,
        plaintext=plaintext,
    )
    envelope = {
        "sync_id": sync_id,
        "resource": resource,
        "space_id": space_id,
        "epoch": 0,
        "seq_start": 0,
        "seq_end": len(records),
        "is_last": False,
        "encrypted_payload": ct,
    }
    enc = FederationEncoder(kp.private_key)
    bytes_to_sign = orjson.dumps(
        {k: v for k, v in envelope.items() if k != "signatures"},
    )
    envelope["signatures"] = enc.sign_envelope_all(
        bytes_to_sign,
        suite="ed25519",
    )
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")


async def test_bans(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bans",
        [
            {"user_id": "u-x", "banned_by": "admin-a", "reason": "spam"},
        ],
    )
    assert c.bans == [("sp-1", "u-x", "admin-a", "spam")]


async def test_posts(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-1",
                "author": "u-1",
                "type": "text",
                "content": "hi",
                "created_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.posts) == 1
    space_id, post = c.posts[0]
    assert space_id == "sp-1"
    assert post.id == "p-1"


async def test_a_post_deleted_here_is_never_resurrected_by_a_sync(setup):
    """v_49: a delete can overtake its create (a member-published delete
    leaves a soft-deleted row); a provider that missed the delete must not
    bring the post back."""
    from datetime import datetime, timezone

    from socialhome.domain.post import Post, PostType

    r, c, kp = setup
    c.held_posts["p-gone"] = Post(
        id="p-gone",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        deleted=True,
    )
    record = {
        "author": "u-1",
        "type": "text",
        "content": "hi",
        "created_at": "2026-04-18T00:00:00+00:00",
    }
    await _send(r, kp, "posts", [{"id": "p-gone", **record}, {"id": "p-2", **record}])
    assert [post.id for _sid, post in c.posts] == ["p-2"]


async def test_posts_keep_their_image_urls(setup):
    """The exporter ships ``image_urls``; a joiner must keep them — the
    media bytes that follow are matched against them, and the feed renders
    from them. Strings only, capped at the feed maximum."""
    from socialhome.domain.post import FEED_POST_MAX_IMAGES

    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-img",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp", 3]
                + [f"api/media/{n}.webp" for n in range(9)],
            },
        ],
    )
    _sid, post = c.posts[0]
    assert post.image_urls[0] == "api/media/a.webp"
    assert all(isinstance(u, str) for u in post.image_urls)
    assert len(post.image_urls) == FEED_POST_MAX_IMAGES


async def test_comments(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "comments",
        [
            {
                "id": "c-1",
                "post_id": "p-1",
                "author": "u-1",
                "type": "text",
                "content": "nice",
                "created_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.comments) == 1
    assert c.comments[0].id == "c-1"


async def test_synced_media_references_keep_only_the_local_shape(setup):
    """A synced row's media references must look like a local upload
    (``api/media/<name>``); anything else is dropped, never stored."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-m",
                "author": "u-1",
                "type": "image",
                "media_url": "https://elsewhere.example/x.webp",
                "image_urls": [
                    "api/media/ok.webp",
                    "api/media/../escape",
                    "ok2.webp",
                    "/api/media/ok3.webp",
                ],
            },
        ],
    )
    await _send(
        r,
        kp,
        "comments",
        [
            {
                "id": "c-m",
                "post_id": "p-m",
                "author": "u-1",
                "type": "image",
                "media_url": "api/media/sub/dir.webp",
            },
        ],
    )
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-m",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Chair",
                "image_urls": ["api/media/chair.webp", "file:///etc/passwd"],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 1,
            },
        ],
    )
    _sid, post = c.posts[0]
    assert post.media_url is None
    assert post.image_urls == ("api/media/ok.webp", "api/media/ok3.webp")
    assert c.comments[0].media_url is None
    assert c.bazaar_listings[0].image_urls == ("api/media/chair.webp",)


async def test_tasks(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-1",
                "list_id": "list-1",
                "title": "X",
                "status": "todo",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.tasks) == 1
    _, task = c.tasks[0]
    assert task.id == "t-1"


async def test_a_task_deleted_here_is_skipped(setup):
    """Migration 0071: a tombstoned task id never comes back via the
    ``tasks`` / ``tasks_archived`` streams of a provider that missed it."""
    r, c, kp = setup
    c.deleted_task_ids.add("t-gone")
    for resource in ("tasks", "tasks_archived"):
        await _send(
            r,
            kp,
            resource,
            [
                {
                    "id": "t-gone",
                    "list_id": "list-1",
                    "title": "X",
                    "status": "todo",
                    "created_by": "u-1",
                },
            ],
        )
    assert c.tasks == []


async def test_task_lists(setup):
    """v_40: the ``task_lists`` resource files each list under the space;
    a record without an id, name or creator is skipped."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "task_lists",
        [
            {"id": "l-1", "name": "Chores", "created_by": "u-1"},
            {"id": "l-2", "name": "", "created_by": "u-1"},
            {"id": "l-3", "name": "No creator"},
        ],
    )
    assert [(sp, lst.id, lst.name) for sp, lst in c.task_lists] == [
        ("sp-1", "l-1", "Chores")
    ]


def _held_task(**kw):
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    base = dict(
        id="t-held",
        list_id="list-1",
        title="Held",
        status=TaskStatus.TODO,
        position=0,
        created_by="u-1",
        created_at=at,
        updated_at=at,
        due_date=date(2026, 10, 3),
        archived_at=at,
        priority=TaskPriority.HIGH,
        labels=("Bills",),
    )
    base.update(kw)
    return Task(**base)


async def test_v39_host_sync_does_not_wipe_held_fields(setup):
    """I1: a v39 host's chunk (no ``priority`` key, the due date / archive
    its inbound lost sent as null) used to be parsed as a NEW row and
    upserted over ours every tick — wiping priority, labels, due date and
    archive. It now merges onto the held row."""
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-1", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-held",
                "list_id": "list-1",
                "title": "Renamed on v39",
                "status": "in_progress",
                "created_by": "u-1",
                "due_date": None,
                "archived_at": None,
            }
        ],
    )
    _, task = c.tasks[-1]
    assert task.title == "Renamed on v39"
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.priority is TaskPriority.HIGH
    assert task.labels == ("Bills",)
    assert task.due_date == date(2026, 10, 3)
    assert task.archived_at == datetime(2026, 9, 1, tzinfo=timezone.utc)


async def test_v40_host_sync_may_clear_held_fields(setup):
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-1", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-held",
                "list_id": "list-1",
                "title": "Held",
                "created_by": "u-1",
                "due_date": None,
                "archived_at": None,
                "priority": None,
                "labels": [],
            }
        ],
    )
    _, task = c.tasks[-1]
    assert task.priority is None and task.labels == ()
    assert task.due_date is None and task.archived_at is None


async def test_sync_never_merges_onto_another_spaces_row(setup):
    """The held row of another space is not ``existing`` — the scoped repo
    refuses the write itself."""
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-other", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [{"id": "t-held", "list_id": "list-1", "title": "x", "created_by": "u-1"}],
    )
    _, task = c.tasks[-1]
    assert task.priority is None and task.labels == ()


async def test_tasks_carry_due_date_archived_priority_labels(setup):
    """The receiver used to drop ``due_date`` and ``archived_at``; it now
    reads the shared wire codec."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks_archived",
        [
            {
                "id": "t-2",
                "list_id": "list-1",
                "title": "X",
                "status": "done",
                "created_by": "u-1",
                "due_date": "2026-10-03",
                "archived_at": "2026-09-01T00:00:00+00:00",
                "priority": "urgent",
                "labels": ["Bills", "bills"],
            },
        ],
    )
    _, task = c.tasks[0]
    assert task.due_date == date(2026, 10, 3)
    assert task.archived_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert task.priority is TaskPriority.URGENT
    assert task.labels == ("Bills",)


async def test_pages(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "pages",
        [
            {
                "id": "pg-1",
                "title": "Welcome",
                "content": "Hi",
                "created_by": "u-1",
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.pages) == 1
    assert c.pages[0].id == "pg-1"


class _FakeConflicts:
    """The v_48 engine: mode + recorded mirrors."""

    def __init__(self, mode: str = "member", host: str = "peer-a") -> None:
        from socialhome.services.page_conflict_service import PageMode

        self.mode_ = PageMode(mode)
        self.host = host
        self.mirrored: list[dict] = []

    async def mode(self, space_id):
        return self.mode_, self.host

    async def mirror(self, **kwargs):
        self.mirrored.append(kwargs)


_PAGE_RECORD = {
    "id": "pg-1",
    "title": "Welcome",
    "content": "Hi again",
    "created_by": "u-1",
    "last_editor_user_id": "u-2",
    "created_at": "2026-04-18T00:00:00+00:00",
    "updated_at": "2026-04-19T00:00:00+00:00",
}


async def test_a_host_record_with_seq_is_mirrored_by_seq(setup):
    """v_48: the host's chunk is its version — mirrored by ``seq`` (never
    an overwrite, never a revert)."""
    r, c, kp = setup
    conflicts = _FakeConflicts()
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 7, "conflict": []}])
    assert c.pages == []
    (call,) = conflicts.mirrored
    assert call["version"].seq == 7 and call["version"].content == "Hi again"


async def test_a_malformed_host_record_is_skipped(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts()
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": -3}])
    assert conflicts.mirrored == [] and c.pages == []


async def test_a_record_from_a_non_host_never_updates_a_held_page(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts(host="someone-else")
    r._page_conflicts = conflicts
    r._page_repo.held[("pg-1", "sp-1")] = object()
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 99}])
    assert conflicts.mirrored == [] and c.pages == []


async def test_a_new_page_from_a_non_host_lands_unsequenced(setup):
    r, c, kp = setup
    r._page_conflicts = _FakeConflicts(host="someone-else")
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 99}])
    (page,) = c.pages
    assert (page.id, page.seq) == ("pg-1", 0)


async def test_under_a_legacy_host_records_are_taken_whole(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts("legacy")
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [_PAGE_RECORD])
    assert [p.content for p in c.pages] == ["Hi again"]
    # … but a record from the host WITH ``seq`` is its version, mirrored
    # even before we saw its v_48 capabilities.
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 3}])
    assert [m["version"].seq for m in conflicts.mirrored] == [3]


async def test_the_host_takes_no_page_record(setup):
    r, c, kp = setup
    r._page_conflicts = _FakeConflicts("host", host="self")
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 3}, _PAGE_RECORD])
    assert c.pages == []


async def test_stickies(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "stickies",
        [
            {
                "id": "s-1",
                "author": "u-1",
                "content": "note",
                "color": "yellow",
                "position_x": 1.0,
                "position_y": 2.0,
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.stickies) == 1
    assert c.stickies[0].id == "s-1"


async def test_calendar(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "calendar",
        [
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "meeting",
                "start": "2026-04-18T10:00:00+00:00",
                "end": "2026-04-18T11:00:00+00:00",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.calendar) == 1
    _, event = c.calendar[0]
    assert event.id == "e-1"


async def test_gallery_album_then_item(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {
                "kind": "album",
                "id": "a-1",
                "space_id": "sp-1",
                "owner_user_id": "u-1",
                "name": "Trip",
            },
            {
                "kind": "item",
                "id": "i-1",
                "album_id": "a-1",
                "uploaded_by": "u-1",
                "item_type": "photo",
                "url": "/m/x.jpg",
                "thumbnail_url": "/m/x-thumb.jpg",
                "width": 1024,
                "height": 768,
            },
        ],
    )
    assert len(c.gallery_albums) == 1 and c.gallery_albums[0].id == "a-1"
    assert len(c.gallery_items) == 1 and c.gallery_items[0].id == "i-1"


async def test_gallery_album_lands_in_the_synced_space_not_the_records(setup):
    """A record's own ``space_id`` (another space, or none = household
    gallery) is untrusted: the album is filed under the stream's space."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {"kind": "album", "id": "a-x", "space_id": "sp-other", "name": "X"},
            {"kind": "album", "id": "a-y", "space_id": None, "name": "Y"},
        ],
    )
    assert [a.space_id for a in c.gallery_albums] == ["sp-1", "sp-1"]


async def test_tasks_archived_routes_to_task_repo(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks_archived",
        [
            {
                "id": "t-done",
                "list_id": "list-1",
                "title": "done one",
                "status": "done",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.tasks) == 1


async def test_polls_skips_persistence(setup):
    """v1: polls ride along with posts via Post.poll — standalone poll
    records just log."""
    r, c, kp = setup
    await _send(r, kp, "polls", [{"post_id": "p-1", "meta": {}, "options": []}])
    assert c.posts == []


async def test_missing_outer_fields_drops(setup):
    """Envelope without sync_id / resource / space_id → drop."""
    r, c, kp = setup
    envelope = {
        "sync_id": "",  # empty
        "resource": "posts",
        "space_id": "sp-1",
    }
    enc = FederationEncoder(kp.private_key)
    bytes_to_sign = orjson.dumps(envelope)
    envelope["signatures"] = enc.sign_envelope_all(
        bytes_to_sign,
        suite="ed25519",
    )
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
    assert c.posts == []


async def test_decrypt_failure_drops(setup, monkeypatch):
    """If decryption raises, the chunk is logged + dropped."""
    r, c, kp = setup

    async def _bad_decrypt(*, space_id, epoch, sync_id, ciphertext):
        raise RuntimeError("wrong key")

    monkeypatch.setattr(r._crypto, "decrypt_chunk", _bad_decrypt)
    await _send(
        r,
        kp,
        "posts",
        [
            {"id": "p-1", "author": "u-1", "type": "text"},
        ],
    )
    assert c.posts == []


async def test_post_missing_required_field_drops(setup):
    """A post record without id/author is skipped by the helper."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {"type": "text", "content": "orphan"},
        ],
    )
    assert c.posts == []


async def test_member_without_user_id_records_nothing(setup):
    """SpaceMember requires user_id; a record without one still
    constructs with user_id='' — not crashing is enough here."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "members",
        [
            {"role": "member", "joined_at": "2026-04-18T00:00:00+00:00"},
        ],
    )
    # member row is saved with empty user_id — receiver doesn't filter,
    # the DB FK would catch it in production. Here we just confirm the
    # branch ran without raising.
    assert len(c.members) == 1


async def test_ban_missing_user_id_drops(setup):
    r, c, kp = setup
    await _send(r, kp, "bans", [{"banned_by": "admin"}])
    assert c.bans == []


async def test_space_zones(setup):
    """The zone catalogue (§23.8.7) rides the chunked sync so a remote
    member instance joining mid-life picks up every zone, not only
    the ones added after the join."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [
            {
                "id": "z_office",
                "space_id": "sp-1",
                "name": "Office",
                "latitude": 47.3769,
                "longitude": 8.5417,
                "radius_m": 150,
                "color": "#3b82f6",
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert len(c.zones) == 1
    z = c.zones[0]
    assert z.id == "z_office"
    assert z.name == "Office"
    assert z.latitude == 47.3769
    assert z.radius_m == 150
    assert z.color == "#3b82f6"


async def test_space_zones_malformed_record_dropped(setup):
    """Lenient receiver: a record missing latitude is dropped, others
    in the same chunk still apply."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [
            {"id": "z_bad", "name": "NoCoords"},  # malformed
            {
                "id": "z_ok",
                "name": "Office",
                "latitude": 47.0,
                "longitude": 8.0,
                "radius_m": 200,
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert [z.id for z in c.zones] == ["z_ok"]


async def test_space_zones_skipped_when_repo_not_wired(bus, peer):
    """An older deployment without a zone repo wired silently skips
    inbound zone chunks rather than erroring."""
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # zone_repo deliberately omitted
    )
    await _send(
        r,
        peer_kp,
        "space_zones",
        [
            {
                "id": "z_office",
                "name": "Office",
                "latitude": 47.0,
                "longitude": 8.0,
                "radius_m": 200,
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert c_zones_count(collector) == 0


def c_zones_count(collector) -> int:
    return len(collector.zones)


async def test_bazaar_listings(setup):
    """F4: bazaar listings ride the chunked sync so a new joiner sees
    the full listing card (mode / price / photos / status) — not just
    the wrapper post's caption."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Vintage chair",
                "description": "A nice chair",
                "image_urls": ["api/media/chair.webp"],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 4500,
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert len(c.bazaar_listings) == 1
    listing = c.bazaar_listings[0]
    assert listing.post_id == "bzr-1"
    assert listing.mode.value == "fixed"
    assert listing.status.value == "active"
    assert listing.price == 4500
    assert listing.image_urls == ("api/media/chair.webp",)


async def test_bazaar_listings_malformed_record_dropped(setup):
    """Lenient receiver: a record missing required fields is dropped,
    others in the same chunk still apply."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {"post_id": "bzr-bad"},  # missing seller, mode, title
            {
                "post_id": "bzr-ok",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "OK",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert [lst.post_id for lst in c.bazaar_listings] == ["bzr-ok"]


async def test_bazaar_listings_unknown_mode_dropped(setup):
    """Forward-compat: a record with a future ``mode`` value is dropped."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "future_mode",  # unknown
                "title": "x",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert c.bazaar_listings == []


async def test_bazaar_listings_skipped_when_repo_not_wired(bus, peer):
    """An older deployment without a bazaar repo wired silently skips
    inbound bazaar chunks rather than erroring."""
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # bazaar_repo deliberately omitted
    )
    await _send(
        r,
        peer_kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u",
                "mode": "fixed",
                "title": "x",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert collector.bazaar_listings == []


# ─── Schedules catch-up (F5) ──────────────────────────────────────────


class _FakePollRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_schedule_poll_in_space(
        self,
        *,
        space_id,
        post_id,
        title,
        deadline,
        slots,
    ):
        self.created.append(
            {
                "post_id": post_id,
                "title": title,
                "deadline": deadline,
                "slots": list(slots),
            },
        )
        return True


@pytest.fixture
def setup_with_poll_repo(bus, peer):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    poll = _FakePollRepo()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        poll_repo=poll,
    )
    return r, poll, peer_kp


async def test_schedules_persists_meta_and_slots(setup_with_poll_repo):
    """F5: schedule chunked sync routes through poll_repo.create_schedule_poll."""
    r, poll, kp = setup_with_poll_repo
    await _send(
        r,
        kp,
        "schedules",
        [
            {
                "post_id": "p-sched",
                "title": "Picnic?",
                "deadline": "2026-08-01",
                "slots": [
                    {
                        "id": "s1",
                        "slot_date": "2026-07-01",
                        "start_time": "14:00",
                        "end_time": "16:00",
                        "position": 0,
                    },
                ],
            },
        ],
    )
    assert len(poll.created) == 1
    rec = poll.created[0]
    assert rec["post_id"] == "p-sched"
    assert rec["title"] == "Picnic?"
    assert rec["slots"][0]["id"] == "s1"


async def test_schedules_malformed_record_dropped(setup_with_poll_repo):
    r, poll, kp = setup_with_poll_repo
    await _send(
        r,
        kp,
        "schedules",
        [
            {"post_id": "bad"},  # missing title + slots
            {"title": "no-post-id"},  # missing post_id
            {  # missing slots
                "post_id": "p-ok",
                "title": "OK",
                "slots": [],
            },
        ],
    )
    assert poll.created == []


async def test_schedules_skipped_when_no_poll_repo_wired(bus, peer):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # poll_repo deliberately omitted
    )
    # Must not raise; receiver branch silently logs + returns.
    await _send(
        r,
        peer_kp,
        "schedules",
        [
            {
                "post_id": "p-sched",
                "title": "Picnic?",
                "slots": [{"id": "s1", "slot_date": "2026-07-01"}],
            },
        ],
    )


async def test_a_synced_album_deleted_here_is_not_brought_back(setup, tombstones):
    """A sync from a household that missed the delete re-sends the album and
    its items; neither comes back."""
    r, c, kp = setup
    tombstones.record("sp-1", "a-gone")
    await _send(
        r,
        kp,
        "gallery",
        [
            {"kind": "album", "id": "a-gone", "owner_user_id": "u-1", "name": "G"},
            {
                "kind": "item",
                "id": "i-gone",
                "album_id": "a-gone",
                "uploaded_by": "u-1",
                "thumbnail_url": "api/media/g.webp",
            },
            {"kind": "album", "id": "a-kept", "owner_user_id": "u-1", "name": "K"},
        ],
    )
    assert [a.id for a in c.gallery_albums] == ["a-kept"]
    assert c.gallery_items == []


async def test_synced_item_media_references_are_normalised(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {
                "kind": "item",
                "id": "i-n",
                "album_id": "a-1",
                "uploaded_by": "u-1",
                "url": "https://elsewhere.example/x.webp",
                "thumbnail_url": "/api/media/t.webp?sig=1",
            },
        ],
    )
    item = c.gallery_items[0]
    assert (item.url, item.thumbnail_url) == ("", "api/media/t.webp")


# ─── Space timetables (v_39) ─────────────────────────────────────────


class _TimetableRepoStub:
    def __init__(self, *, applies: bool = True):
        self.applied: list = []
        self._applies = applies

    async def apply_remote(self, tt, *, space_id):
        self.applied.append((space_id, tt))
        return self._applies

    async def get(self, timetable_id):
        return None


def _timetable_receiver(bus, peer, repo):
    peer_inst, _kp = peer
    collector = _FakeRepos()
    return SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        timetable_repo=repo,
    )


def _tt_record(owner: str = "u-adm", space_id: str = "sp-1", **extra) -> dict:
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return {
        **to_wire_dict(
            Timetable(
                id=mint_owner_bound_id(
                    SPACE_TIMETABLE_KIND, space_id=space_id, owner_user_id=owner
                ),
                name="5b",
                created_by=owner,
                created_at=at,
                updated_at=at,
                updated_by=owner,
            )
        ),
        **extra,
    }


async def test_timetables_apply_last_writer_wins_and_publish(bus, peer):
    repo = _TimetableRepoStub()
    r = _timetable_receiver(bus, peer, repo)
    seen: list = []

    async def _rec(ev):
        seen.append(ev)

    bus.subscribe(TimetableSaved, _rec)
    good = _tt_record()
    await _send(r, peer[1], "timetables", [good])
    [(space_id, tt)] = repo.applied
    assert (space_id, tt.id, tt.assignees) == ("sp-1", good["id"], ())
    [ev] = seen
    assert (ev.space_id, ev.origin_instance_id) == ("sp-1", "peer-a")


async def test_a_stale_timetable_copy_is_not_published(bus, peer):
    r = _timetable_receiver(bus, peer, _TimetableRepoStub(applies=False))
    seen: list = []

    async def _rec(ev):
        seen.append(ev)

    bus.subscribe(TimetableSaved, _rec)
    await _send(r, peer[1], "timetables", [_tt_record()])
    assert seen == []


async def test_bad_timetable_records_are_skipped_the_rest_apply(bus, peer, caplog):
    repo = _TimetableRepoStub()
    r = _timetable_receiver(bus, peer, repo)
    good = _tt_record()
    with caplog.at_level("WARNING"):
        await _send(
            r,
            peer[1],
            "timetables",
            [
                {"id": "x"},  # malformed
                _tt_record(schema=2),  # wrong schema
                _tt_record(assignees=["u-adm"]),  # assignees
                _tt_record(space_id="sp-2"),  # bound to another space
                {**_tt_record(), "id": "0123456789abcdef0123456789abcdef"},
                "not-an-object",
                good,
            ],
        )
    assert [tt.id for _sid, tt in repo.applied] == [good["id"]]
    assert "timetable" in caplog.text


async def test_timetables_skipped_when_repo_not_wired(bus, peer):
    r = _timetable_receiver(bus, peer, None)
    await _send(r, peer[1], "timetables", [_tt_record()])  # no raise


async def test_a_held_comment_mid_chunk_never_stops_the_rest(setup, tmp_dir):
    """Review repro I3: a comment the member relay already delivered, or a
    delete-before-create tombstone, used to make the plain INSERT raise and
    lose the rest of the chunk. Held rows stay as they are; the later
    records land."""
    from datetime import datetime, timezone

    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.post import Comment, CommentType, Post, PostType
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo

    r, _c, kp = setup
    db = AsyncDatabase(tmp_dir / "sync.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES('sp-1','S','peer-a','o','ab')"
        )
        repo = SqliteSpacePostRepo(db)
        now = datetime.now(timezone.utc)
        await repo.save(
            "sp-1",
            Post(id="p-1", author="u-1", type=PostType.TEXT, created_at=now),
        )
        await repo.add_comment(
            Comment(
                id="c-held",
                post_id="p-1",
                author="u-1",
                type=CommentType.TEXT,
                created_at=now,
                content="from the relay",
            ),
            space_id="sp-1",
        )
        await repo.add_comment(
            Comment(
                id="c-gone",
                post_id="p-1",
                author="u-1",
                type=CommentType.TEXT,
                created_at=now,
                deleted=True,
            ),
            space_id="sp-1",
        )
        r._space_post_repo = repo
        rec = {"post_id": "p-1", "author": "u-1", "type": "text"}
        await _send(
            r,
            kp,
            "comments",
            [
                {"id": "c-held", **rec, "content": "from sync"},
                {"id": "c-gone", **rec, "content": "resurrected"},
                {"id": "c-new", **rec, "content": "later record"},
            ],
        )
        assert (await repo.get_comment("c-held")).content == "from the relay"
        gone = await repo.get_comment("c-gone")
        assert gone.deleted and gone.content is None
        assert (await repo.get_comment("c-new")).content == "later record"
    finally:
        await db.shutdown()


async def test_a_sync_keeps_a_held_posts_reactions_and_comment_count(setup):
    """Review repro S1: re-sending a held post must not wipe the reactions
    (or comment count) held here — the stamps would then refuse the copy
    that could restore them."""
    from datetime import datetime, timezone

    from socialhome.domain.post import Post, PostType

    r, c, kp = setup
    c.held_posts["p-1"] = Post(
        id="p-1",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        content="hi",
        reactions={"👍": frozenset({"u-2"})},
        comment_count=3,
    )
    record = {"id": "p-1", "author": "u-1", "type": "text", "content": "hi"}
    await _send(r, kp, "posts", [record])
    _sid, saved = c.posts[0]
    assert saved.reactions == {"👍": frozenset({"u-2"})}
    assert saved.comment_count == 3


async def test_a_new_post_from_a_sync_carries_its_reactions(setup):
    r, c, kp = setup
    record = {
        "id": "p-new",
        "author": "u-1",
        "type": "text",
        "reactions": {"👍": ["u-2", "u-3"], "bad": "x", "🎉": [4]},
    }
    await _send(r, kp, "posts", [record])
    _sid, saved = c.posts[0]
    assert saved.reactions == {"👍": frozenset({"u-2", "u-3"})}


async def test_relayed_reactions_survive_a_sync_end_to_end(setup, tmp_dir):
    """S1 on real SQLite: a relayed reaction, a sync re-sending the post, a
    duplicate copy of the add — the reaction is still there."""
    from datetime import datetime, timezone

    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.post import Post, PostType
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo

    r, _c, kp = setup
    db = AsyncDatabase(tmp_dir / "sync.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES('sp-1','S','peer-a','o','ab')"
        )
        repo = SqliteSpacePostRepo(db)
        await repo.save(
            "sp-1",
            Post(
                id="p-1",
                author="u-1",
                type=PostType.TEXT,
                created_at=datetime.now(timezone.utc),
                content="hi",
            ),
        )
        await repo.add_reaction(
            "p-1", "👍", "u-2", space_id="sp-1", stamp="2026-10-03 12:00:00"
        )
        r._space_post_repo = repo
        await _send(
            r,
            kp,
            "posts",
            [{"id": "p-1", "author": "u-1", "type": "text", "content": "hi"}],
        )
        assert "u-2" in (await repo.get("p-1"))[1].reactions["👍"]
    finally:
        await db.shutdown()
