"""Shared fakes of the space-sync receiver tests (their fixtures are in
``conftest.py``)."""

from __future__ import annotations

from types import SimpleNamespace

import orjson

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import InstanceSource, PairingStatus, RemoteInstance
from socialhome.domain.space_media_ref import SpaceMediaLocalization
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.exporter import serialise_chunk
from socialhome.services.inbound_media_store import space_direct_media_name


class _FakeCrypto:
    def __init__(self) -> None:
        self.epoch = 0

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return self.epoch, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        import base64

        return base64.urlsafe_b64decode(ciphertext)


class _FakeFedRepo:
    def __init__(self, peer):
        self._peer = peer

    async def get_instance(self, iid):
        return self._peer if (self._peer and self._peer.id == iid) else None


class _FakeSpaceRepo:
    def __init__(self):
        self.members = []
        self.bans = []
        #: space_id → host household identity pubkey (hex). Populated on a
        #: stub whose host we are NOT paired with; the receiver falls back
        #: to it to verify mesh-routed chunk signatures (#648).
        self.host_identity_pks: dict[str, str] = {}
        #: space_id → Space (or a stand-in exposing owner_instance_id).
        self.spaces: dict[str, object] = {}
        #: (space_id, instance_id) → a member household's recorded identity
        #: key (``space_instances.identity_pk``).
        self.instance_pks: dict[tuple[str, str], str] = {}

    async def get_host_identity_pk(self, space_id):
        return self.host_identity_pks.get(space_id)

    async def get_space_instance_identity_pk(self, space_id, instance_id):
        return self.instance_pks.get((space_id, instance_id))

    async def set_host_identity_pk(self, space_id, pk_hex):
        self.host_identity_pks[space_id] = pk_hex

    async def get(self, space_id):
        # Unless a test says otherwise, ``peer-a`` hosts the space — its
        # chunks are taken whole (non-host providers are covered by the
        # authorship protocol tests).
        return self.spaces.get(space_id) or SimpleNamespace(
            id=space_id,
            owner_instance_id="peer-a",
            archived=False,
            archived_reason=None,
            dissolved=False,
        )

    async def save_member(self, member):
        self.members.append(member)
        return member

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.bans.append((space_id, user_id, banned_by, reason))


class _FakeSpacePostRepo:
    def __init__(self):
        self.saved = []
        self.comments = []

    async def save(self, space_id, post):
        self.saved.append((space_id, post))
        return post

    async def get(self, post_id):
        return None

    async def add_comment(self, comment, *, space_id):
        self.comments.append(comment)
        return True


class _Stub:
    """Generic stub with a list of saved items."""

    def __init__(self):
        self.saved = []

    async def save(self, obj, *args):
        self.saved.append((obj, args) if args else obj)
        return obj

    async def save_event(self, space_id, event):
        self.saved.append((space_id, event))
        return event

    async def create_album(self, album):
        self.saved.append(("album", album))

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        self.saved.append(("item", item))
        return True

    async def get_system_album(self, space_id):
        return None

    async def get_album(self, album_id):
        return None

    async def recount_items(self, album_id):
        return 0

    async def is_album_deleted(self, album_id, *, space_id):
        return False

    async def is_item_deleted(self, item_id, *, space_id):
        return False


def _make_peer() -> tuple[RemoteInstance, object]:
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id="peer-a",
        display_name="Peer A",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer/wh",
        local_inbox_id="wh-peer-a",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    return peer, kp


async def _sign_as_peer(kp, envelope):
    """Sign an envelope as if we were the peer."""
    encoder = FederationEncoder(kp.private_key)
    bytes_to_sign = orjson.dumps(
        {k: v for k, v in envelope.items() if k != "signatures"}
    )
    envelope["signatures"] = encoder.sign_envelope_all(
        bytes_to_sign,
        suite="ed25519",
    )
    return envelope


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
        self.album_edits = []
        self.item_edits = []
        self.recounted = []
        self.gallery_items = []
        self.zones = []
        self.bazaar_listings = []
        #: ``(space_id, album_id)`` of the album tombstones held here.
        self.deleted_albums: set[tuple[str, str]] = set()

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
            self.pages.append(args[0]) if (args[0] is None) is False and hasattr(
                args[0], "title"
            ) else self.stickies.append(args[0])
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

    async def is_deleted(self, sticky_id, *, space_id):
        return False


class _CalendarRepoStub:
    def __init__(self, collector):
        self._c = collector
        #: Events "held here", by id: ``(space_id, event)``.
        self.held: dict[str, tuple] = {}

    async def save_event(self, event, *, space_id):
        self._c.calendar.append((space_id, event))
        return True

    async def is_event_deleted(self, event_id, *, space_id):
        return False

    async def get_event(self, event_id):
        return self.held.get(event_id)


class _GalleryRepoStub:
    def __init__(self, collector):
        self._c = collector

    async def create_album(self, album):
        self._c.gallery_albums.append(album)

    async def get_system_album(self, space_id):
        return None

    async def get_album(self, album_id):
        return next((a for a in self._c.gallery_albums if a.id == album_id), None)

    async def update_album_in_space(self, album_id, patch, *, space_id):
        self._c.album_edits.append((album_id, patch))
        return True

    async def recount_items(self, album_id):
        self._c.recounted.append(album_id)
        return sum(1 for i in self._c.gallery_items if i.album_id == album_id)

    async def get_item(self, item_id):
        return next((i for i in self._c.gallery_items if i.id == item_id), None)

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        if await self.get_item(item.id) is None:
            self._c.gallery_items.append(item)
        return True

    async def edit_item_in_space(self, item_id, *, space_id, patch, to_album_id=None):
        self._c.item_edits.append((item_id, patch, to_album_id))
        held = await self.get_item(item_id)
        return held.album_id if held is not None else None

    async def is_album_deleted(self, album_id, *, space_id):
        return (space_id, album_id) in self._c.deleted_albums

    async def is_item_deleted(self, item_id, *, space_id):
        return False


class _ZoneRepoStub:
    def __init__(self, collector):
        self._c = collector
        self.stored: dict = {}

    async def get(self, zone_id):
        return self.stored.get(zone_id)

    async def upsert(self, zone, *, space_id):
        self._c.zones.append(zone)
        self.stored[zone.id] = zone
        return True

    async def is_deleted(self, zone_id, *, space_id):
        return False


class _BazaarRepoStub:
    """Minimal AbstractBazaarRepo slice for receiver tests (F4)."""

    def __init__(self, collector):
        self._c = collector

    async def save_listing(self, listing, *, space_id):
        self._c.bazaar_listings.append(listing)
        return True

    async def get_listing(self, post_id):
        return None


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

    async def get_member(self, space_id, user_id):
        return SimpleNamespace(user_id=user_id) if user_id == "u-seated" else None

    # ``holds_member_seat``: this household's one local user ``u-local``
    # holds a member's seat, or only a follower's (``follower_only``).
    async def list_local_member_user_ids(self, space_id):
        return ["u-local"]

    async def list_members(self, space_id):
        role = "subscriber" if getattr(self._c, "follower_only", False) else "member"
        return [SimpleNamespace(user_id="u-local", role=role)]


def _here(name: str, space_id: str = "sp-1") -> str:
    """The reference a file streamed by ``peer-a`` takes here: its name as
    delivered by that household (``SpaceMediaNamer``)."""
    return f"api/media/{space_direct_media_name(space_id, 'peer-a', name)}"


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


class _GfsMediaSink:
    """Renames each described file ``sg-<name>`` (the reader's own name);
    records what was localized and what was kept."""

    def __init__(self):
        self.received: list[tuple] = []
        self.kept: list[dict] = []
        self.as_shown: list[tuple] = []
        #: item → the hash map the receiver passed (v_85).
        self.hashes: dict[str, object] = {}

    async def localize(
        self, space_id, raw_refs, media_urls, *, item_id, origin, hashes=None
    ):
        self.hashes[item_id] = hashes
        urls = [u for u in media_urls if u]
        if isinstance(raw_refs, dict):
            self.received.append((space_id, raw_refs, urls, item_id, origin))
        described = raw_refs if isinstance(raw_refs, dict) else {}
        names = {
            u.rsplit("/", 1)[-1]: "sg-" + u.rsplit("/", 1)[-1]
            for u in urls
            if u.rsplit("/", 1)[-1] in described
        }
        return SpaceMediaLocalization(names=names, refs=(object(),) if names else ())

    def localize_as_shown(self, space_id, raw_refs, media_urls, *, item_id, origin):
        self.as_shown.append((space_id, raw_refs, list(media_urls), item_id, origin))
        return SpaceMediaLocalization(refs=(object(),))

    async def keep(self, loc, *, prefetch=True):
        if loc.refs:
            self.kept.append(dict(loc.names))
        return []


_SYNC_REFS = {"a.webp": {"blob_id": "a" * 64, "suite": "s", "epoch": 0, "size": 9}}
