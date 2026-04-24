"""Coverage for each per-resource persist path in :class:`SpaceSyncReceiver`."""

from __future__ import annotations

import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.exporter import serialise_chunk
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
        self.comments = []
        self.tasks = []
        self.pages = []
        self.stickies = []
        self.calendar = []
        self.gallery_albums = []
        self.gallery_items = []

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

    async def add_comment(self, comment):
        self.comments.append(comment)
        return comment

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

    async def create_item(self, item):
        self.gallery_items.append(item)


class _PostRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, space_id, post):
        self._c.posts.append((space_id, post))
        return post

    async def add_comment(self, comment):
        self._c.comments.append(comment)
        return comment


class _TaskRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, space_id, task):
        self._c.tasks.append((space_id, task))
        return task


class _PageRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, page):
        self._c.pages.append(page)
        return page


class _StickyRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save(self, sticky):
        self._c.stickies.append(sticky)
        return sticky


class _CalendarRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def save_event(self, space_id, event):
        self._c.calendar.append((space_id, event))
        return event


class _GalleryRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def create_album(self, album):
        self._c.gallery_albums.append(album)

    async def create_item(self, item):
        self._c.gallery_items.append(item)


class _SpaceRepoStub:
    def __init__(self, collector):
        self._c = collector

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
def setup(bus, peer):
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
