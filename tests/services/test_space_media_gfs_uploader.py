"""Seal a space item's files once and upload them once per GFS
(:mod:`socialhome.services.space_media_gfs_uploader`)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import orjson
import pytest

from socialhome.canonical_json import canonical_bytes
from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.domain.federation import InstanceSource
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.gfs_channel import CHANNEL_SUITE_ED25519
from socialhome.domain.gfs_space_media import (
    AUTH_CHANNEL,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_PUT_PREFIX,
    PIECE_BYTES,
    PieceProof,
    SpaceMediaUploadAuth,
    check_piece,
    space_media_address,
)
from socialhome.domain.space import SpaceType
from socialhome.domain.space_item_outbox import (
    MEDIA_UPLOAD_ROW,
    UNPLANNED_GFS,
    QueuedSpaceItem,
)
from socialhome.gfs_channel import channel_pk_of, derive_channel_seed
from socialhome.moment_public_media import blob_id_of
from socialhome.services import space_media_gfs_uploader as up_mod
from socialhome.services.space_media_gfs_uploader import SpaceMediaGfsUploader
from socialhome.space_media_seal import (
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    derive_space_media_key,
    open_space_media,
)

SPACE = "sp-1"
MEDIA_KEY = derive_space_media_key(bytes(range(32)))
#: The item planned and this household (the sealer: both in the AAD).
ITEM = "post-1"
OWN = "own-inst"
CHANNEL_ID = "c" * 32
#: Default for ``_Spaces(channel=…)``: the channel the seed derives.
_SEED_CHANNEL = object()


@dataclass
class _Conn:
    id: str
    inbox_url: str
    registered: bool = True


@dataclass
class _Instance:
    id: str
    source: InstanceSource
    relay_via: str | None = None


class _Gfs:
    def __init__(
        self,
        conns,
        *,
        max_blob=9_000_000,
        follower=(),
        status=201,
        scopes=("channel",),
        ttl_s=259_200,
        pieces=True,
    ):
        self.pieces = pieces
        self.conns = {c.inbox_url: c for c in conns}
        self.max_blob = max_blob
        self.follower = list(follower)
        self.status = status
        self.scopes = set(scopes)
        self.ttl_s = ttl_s
        self.puts: list[tuple[str, str, str, bytes, str, bool]] = []
        self.put_scopes: list[str] = []

    async def space_media_ttl_s(self, conn):
        return self.ttl_s

    async def space_media_pieces(self, conn):
        return self.pieces

    async def space_media_file_max(self, conn):
        return 128 * 1024 * 1024 if self.pieces else 0

    def space_media_cache_off(self, conn) -> bool:
        return False

    def capability_unknown(self, conn) -> bool:
        return getattr(self, "unknown", False)

    async def space_media_max_blob(self, conn, *, scope="channel"):
        if scope not in self.scopes:
            return None
        if isinstance(self.max_blob, dict):
            return self.max_blob.get(conn.id)
        return self.max_blob

    async def connection_for_url(self, url):
        return self.conns.get(url)

    async def space_media_follower_targets(self, space_id):
        return self.follower

    async def put_space_media(
        self, conn, *, blob_id, blob, auth_header, piece_proof=None
    ):
        # The claim as the GFS reads it (decoded here without validating a
        # stub cert); the address must be the scope-bound one — or, for a
        # piece, the one its proof places it at (the GFS's own check).
        claim = orjson.loads(
            base64.urlsafe_b64decode(auth_header + "=" * (-len(auth_header) % 4))
        )
        assert blob_id == claim["blob_id"]
        if piece_proof is not None:
            assert claim["addr_suite"] == "sm1p"
            assert check_piece(
                claim["scope"],
                claim["scope_id"],
                blob_id,
                blob,
                PieceProof.from_header(piece_proof),
            )
        else:
            assert blob_id == space_media_address(
                claim["scope"], claim["scope_id"], blob
            )
            assert claim["addr_suite"] == "sm1"
        self.put_scopes.append(claim["scope"])
        self.puts.append((conn.id, claim["scope_id"], blob_id, blob, auth_header))
        status = self.status
        if isinstance(status, list):
            return status.pop(0)
        return status


class _FedRepo:
    def __init__(self, instances):
        self.instances = {i.id: i for i in instances}

    async def list_member_instance_ids(self, space_id):
        return list(self.instances)

    async def get_instance(self, iid):
        return self.instances.get(iid)


class _Fed:
    own_instance_id = OWN

    def __init__(self, versions, frames=()):
        self.versions = versions
        self.frames = set(frames)

    async def peer_supports(self, iid, *, min_version):
        return self.versions.get(iid, 0) >= min_version

    async def relay_media_ok(self, instance):
        return instance.id in self.frames

    async def space_member_supports(self, iid, *, min_version):
        return self.versions.get(iid, 0) >= min_version


class _Crypto:
    async def media_key(self, space_id, *, epoch=None):
        if space_id != SPACE:
            return None
        return (epoch if epoch is not None else 4), MEDIA_KEY

    async def get_current_epoch(self, space_id):
        return 4


class _Spaces:
    def __init__(
        self,
        seed=None,
        *,
        channel=_SEED_CHANNEL,
        private_gfs=False,
        owner=OWN,
        readable=True,
    ):
        self.seed = seed
        self.owner = owner
        #: Followers on (the space has a writer group key) or off.
        self.readable = readable
        #: ``(channel_id, channel_pk)`` of the space's opaque channel — by
        #: default the one ``seed`` derives (since v_96 the only scope a GFS
        #: keeps space media under); ``None`` for a space with no channel.
        if channel is _SEED_CHANNEL:
            channel = (
                (
                    CHANNEL_ID,
                    channel_pk_of(derive_channel_seed(seed, SPACE, CHANNEL_ID)),
                )
                if seed
                else None
            )
        self.channel = channel
        self.private_gfs = private_gfs

    async def get_space_seed(self, space_id):
        return self.seed

    async def get(self, space_id):
        if not self.private_gfs:
            return SimpleNamespace(
                space_type=SpaceType.GLOBAL,
                features=SimpleNamespace(
                    private_gfs=False, allow_subscribers=self.readable
                ),
                owner_instance_id=self.owner,
            )
        return SimpleNamespace(
            space_type=SpaceType.PRIVATE,
            features=SimpleNamespace(private_gfs=True),
            owner_instance_id=self.owner,
        )

    async def get_gfs_channel(self, space_id):
        return self.channel


@dataclass
class _Cert:
    scope: str = "write"
    instance_pk: str = ""

    def to_wire(self):
        return {
            "cert_suite": "ed25519",
            "space_id": SPACE,
            "epoch": 4,
            "instance_pk": self.instance_pk,
            "scope": self.scope,
            "issued_at": 1,
            "cert_sig": "x",
        }


def _write(media_dir: pathlib.Path, name: str, size: int) -> bytes:
    data = os.urandom(size)
    (media_dir / name).write_bytes(data)
    return data


class _MediaRefs:
    """Which files are the space's own content (``confined_to_space``)."""

    def __init__(self, foreign=()):
        self.foreign = set(foreign)
        self.asked: list[tuple[str, str]] = []

    async def confined_to_space(self, basename, space_id):
        self.asked.append((basename, space_id))
        return basename not in self.foreign


#: The owner's space seed the helper defaults to (so planning has an upload
#: authorization); pass ``seed=None`` for a household that holds none.
OWNER_SEED = b"\x01" * 32
_DEFAULT = object()


def _uploader(
    tmp_path,
    *,
    gfs,
    instances=(),
    versions=None,
    seed=_DEFAULT,
    spaces=None,
    media_refs=None,
):
    u = SpaceMediaGfsUploader(
        space_repo=spaces or _Spaces(OWNER_SEED if seed is _DEFAULT else seed),
        space_crypto=_Crypto(),
        federation_repo=_FedRepo(instances),
        gfs_service=gfs,
        media_refs=media_refs or _MediaRefs(),
        media_dir=tmp_path,
    )
    u.attach_federation(_Fed(versions or {}))
    return u


RELAY = _Instance("e", InstanceSource.SPACE_SESSION, relay_via="https://g1.example")
PAIRED = _Instance("b", InstanceSource.MANUAL)
V = FederationCapability.MIN_FOR_SPACE_MEDIA_REFS


# ── Who reads through a GFS ──────────────────────────────────────────────


async def test_relay_reader_needs_a_link_seat_at_v80(tmp_path):
    u = _uploader(
        tmp_path,
        gfs=_Gfs([]),
        instances=[RELAY, PAIRED, _Instance("old", InstanceSource.SPACE_SESSION)],
        versions={"e": V, "b": V, "old": V - 1},
    )
    assert await u.is_relay_reader("e") is True
    assert await u.is_relay_reader("b") is False
    assert await u.is_relay_reader("old") is False
    assert await u.is_relay_reader("nobody") is False


async def test_takes_relay_frames_asks_the_federation_for_the_seat_row(tmp_path):
    """A seat whose relay carries binary frames (v_87) takes the media push
    like any member; an unknown household or one without frames does not."""
    u = _uploader(tmp_path, gfs=_Gfs([]), instances=[RELAY, PAIRED])
    u.attach_federation(_Fed({}, frames={"e"}))
    assert await u.takes_relay_frames("e") is True
    assert await u.takes_relay_frames("b") is False
    assert await u.takes_relay_frames("nobody") is False
    no_fed = SpaceMediaGfsUploader(
        space_repo=_Spaces(None),
        space_crypto=_Crypto(),
        federation_repo=_FedRepo([RELAY]),
        gfs_service=_Gfs([]),
        media_refs=_MediaRefs(),
        media_dir=tmp_path,
    )
    assert await no_fed.takes_relay_frames("e") is False


# ── Planning ─────────────────────────────────────────────────────────────


async def test_no_gfs_planned_yet_still_describes_and_queues_unplanned(tmp_path):
    """Review F finding 4 / owner B-7: a space with a channel but no GFS
    connection right now still describes its files; the upload waits in the
    durable queue as one unplanned row per blob (planned when due) — never
    filtered and lost."""
    _write(tmp_path, "a.webp", 1000)
    u = _uploader(tmp_path, gfs=_Gfs([]), instances=[PAIRED], versions={"b": V})
    box = _Outbox()
    u.attach_outbox(box)
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    assert plan is not None and plan.targets == ()
    assert set(plan.wire()) == {"a.webp"}
    await u.upload(plan, author_user_id="anna")
    assert [r.gfs_id for r in box.rows] == [UNPLANNED_GFS]
    assert box.failed == []


async def test_plan_for_a_relay_seat_describes_each_file(tmp_path):
    a = _write(tmp_path, "a.webp", 1000)
    _write(tmp_path, "b.webp", 70_000)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    plan = await u.plan(
        SPACE,
        ["/api/media/a.webp?sig=x", "api/media/b.webp", "https://evil/x.webp"],
        item_id=ITEM,
        followers=False,
    )
    assert plan is not None
    assert [t.id for t in plan.targets] == ["g1"]
    wire = plan.wire()
    assert set(wire) == {"a.webp", "b.webp"}
    entry = wire["a.webp"]
    assert entry["suite"] == SPACE_MEDIA_SUITE_AESGCM256_PADDED
    assert entry["epoch"] == 4
    ref = next(r for r in plan.refs if r.filename == "a.webp")
    # The descriptor's address is the deterministic blob of the file.
    blob = up_mod.seal_file_sync(MEDIA_KEY, SPACE, 4, ITEM, OWN, "a.webp", a)
    # The address is bound to the space scope (sm1), not the bare hash.
    assert (
        entry["blob_id"]
        == space_media_address("channel", CHANNEL_ID, blob)
        == ref.blob_id
    )
    assert entry["blob_id"] != blob_id_of(blob)
    assert (entry["scope"], entry["addr_suite"]) == ("channel", "sm1")
    assert entry["size"] == len(blob)


async def test_followers_target_the_channel_gfs_set(tmp_path):
    """v_96 — a global space's followers read through its channel: their
    GFSs are planned (under the channel scope) only once it runs one."""
    _write(tmp_path, "a.webp", 10)
    g2 = _Conn("g2", "https://g2.example")
    cid = "c" * 32
    pk = channel_pk_of(derive_channel_seed(OWNER_SEED, SPACE, cid))
    u = _uploader(
        tmp_path,
        gfs=_Gfs([], follower=[g2]),
        spaces=_Spaces(OWNER_SEED, channel=(cid, pk)),
    )
    unplanned = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    assert unplanned is not None and unplanned.targets == ()
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True)
    assert [t.id for t in plan.targets] == ["g2"]


async def test_a_global_space_without_a_channel_uploads_nothing_for_followers(
    tmp_path,
):
    """v_96 — no channel here, no follower reads through a GFS: nothing is
    planned (or uploaded) space-scoped for them, so no request names the
    space to a listing GFS for a reader that does not exist."""
    _write(tmp_path, "a.webp", 10)
    g2 = _Conn("g2", "https://g2.example")
    gfs = _Gfs([], follower=[g2])
    u = _uploader(tmp_path, gfs=gfs, spaces=_Spaces(b"\2" * 32, channel=None))
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True) is None
    )
    assert gfs.puts == []


async def test_a_gfs_without_the_capability_is_left_out(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], max_blob=None)
    u = _uploader(tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V})
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False) is None
    )


async def test_one_gfs_shared_by_two_readers_is_one_target(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    e2 = _Instance("e2", InstanceSource.SPACE_SESSION, relay_via="https://g1.example")
    u = _uploader(
        tmp_path,
        gfs=_Gfs([g1], follower=[g1]),
        instances=[RELAY, e2],
        versions={"e": V, "e2": V},
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True)
    assert [t.id for t in plan.targets] == ["g1"]


async def test_too_large_missing_and_no_key_files_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(up_mod, "MAX_SPACE_MEDIA_BYTES", 100)
    _write(tmp_path, "big.mp4", 101)
    _write(tmp_path, "ok.webp", 100)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    plan = await u.plan(
        SPACE,
        ["api/media/big.mp4", "api/media/ok.webp", "api/media/gone.webp"],
        item_id=ITEM,
        followers=False,
    )
    assert set(plan.wire()) == {"ok.webp"}
    # A space we hold no key for: nothing to describe.
    assert await u._describe_names("other", ["ok.webp"], ITEM) == []


async def test_plan_for_one_reader_targets_only_its_gfs(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    g2 = _Conn("g2", "https://g2.example")
    u = _uploader(
        tmp_path,
        gfs=_Gfs([g1, g2], follower=[g2]),
        instances=[RELAY],
        versions={"e": V},
    )
    plan = await u.plan_for_reader(SPACE, ["api/media/a.webp"], "e", item_id=ITEM)
    assert [t.id for t in plan.targets] == ["g1"]
    assert (
        await u.plan_for_reader(SPACE, ["api/media/a.webp"], "nobody", item_id=ITEM)
        is None
    )


# ── Uploading ────────────────────────────────────────────────────────────


class _Outbox:
    """The durable queue as the uploader sees it: rows it queues, failures
    it records."""

    def __init__(self) -> None:
        self.rows: list[QueuedSpaceItem] = []
        self.failed: list[tuple[str, str, str]] = []

    async def queue(
        self,
        *,
        space_id,
        epoch,
        payload,
        event_type,
        ring_class,
        author_user_id,
        gfs_ids,
        delay_s=0.0,
        supersede_key=None,
    ):
        assert event_type == MEDIA_UPLOAD_ROW
        for gfs_id in gfs_ids:
            self.rows.append(
                QueuedSpaceItem(
                    item_id=f"r{len(self.rows)}",
                    gfs_id=gfs_id,
                    space_id=space_id,
                    epoch=epoch,
                    ring_class=ring_class,
                    event_type=event_type,
                    author_user_id=author_user_id,
                    payload=payload,
                    next_attempt_at="",
                    created_at="",
                )
            )
        return True

    async def fail(self, *, space_id, event_type, author_user_id, error):
        self.failed.append((space_id, author_user_id, error))

    async def queue_media(self, *, space_id, epoch, parts, author_user_id):
        for payload, gfs_ids in parts:
            await self.queue(
                space_id=space_id,
                epoch=epoch,
                payload=payload,
                event_type=MEDIA_UPLOAD_ROW,
                ring_class="durable",
                author_user_id=author_user_id,
                gfs_ids=gfs_ids,
            )
        return True


async def _upload(u, plan, *, tries=6, author=""):
    """Queue *plan* and run its rows the way the durable queue does (a
    transient answer is tried again); the outcome of each row."""
    box = u._outbox if isinstance(u._outbox, _Outbox) else _Outbox()
    u.attach_outbox(box)
    start = len(box.rows)
    await u.upload(plan, author_user_id=author)
    outcomes = []
    for row in box.rows[start:]:
        for _ in range(tries):
            out = await u.deliver(row)
            if out.kind != "transient":
                break
        outcomes.append(out.kind)
    return outcomes


async def test_the_owner_uploads_with_the_channel_key(tmp_path):
    data = _write(tmp_path, "a.webp", 5000)
    kp = generate_identity_keypair()
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        seed=kp.private_key,
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, plan)
    assert len(gfs.puts) == 1
    conn_id, space_id, blob_id, blob, header = gfs.puts[0]
    assert (conn_id, space_id) == ("g1", CHANNEL_ID)
    assert (
        blob_id
        == space_media_address("channel", CHANNEL_ID, blob)
        == plan.refs[0].blob_id
    )
    assert auth_claim_addr_suite(header) == "sm1"
    assert (
        open_space_media(
            blob=blob,
            media_key=MEDIA_KEY,
            space_id=SPACE,
            epoch=4,
            item_id=ITEM,
            origin=OWN,
            filename="a.webp",
        )
        == data
    )
    auth = SpaceMediaUploadAuth.from_header(header)
    assert auth.kind == AUTH_CHANNEL
    assert auth.claim.blob_id == blob_id and auth.claim.size == len(blob)
    assert auth.claim.expires_at > auth.claim.ts
    channel_seed = derive_channel_seed(kp.private_key, SPACE, CHANNEL_ID)
    assert verify_ed25519(
        b64url_decode(channel_pk_of(channel_seed)),
        CHANNEL_MEDIA_PUT_PREFIX + canonical_bytes(auth.claim.signing_body()),
        b64url_decode(auth.sig),
    )


async def test_a_writer_cert_alone_uploads_nothing(tmp_path):
    """v_90 — no identified upload: a writer cert without the writer key
    (an older household) authorizes nothing."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        seed=None,
    )
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    assert gfs.puts == []
    assert not await u.can_upload(SPACE)


async def test_without_an_upload_authorization_nothing_is_sealed_or_described(
    tmp_path,
):
    """A plain member of a space it cannot upload for (no seed, no channel
    grant): no plan — no blob is sealed and no ``media_refs`` names a blob
    that will never be on the GFS; its link-joined readers get the push."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(
        tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V}, seed=None
    )
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True) is None
    )


async def test_no_authorization_uploads_nothing(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        seed=None,
    )
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    assert gfs.puts == []


async def test_already_stored_is_success_and_a_repeat_is_not_resent(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=200)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, plan)
    await _upload(u, plan)
    assert len(gfs.puts) == 1


async def test_two_gfs_get_one_upload_each(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    g2 = _Conn("g2", "https://g2.example")
    e2 = _Instance("e2", InstanceSource.SPACE_SESSION, relay_via="https://g2.example")
    gfs = _Gfs([g1, g2])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY, e2],
        versions={"e": V, "e2": V},
        seed=b"\2" * 32,
    )
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True)
    )
    assert sorted(p[0] for p in gfs.puts) == ["g1", "g2"]
    assert gfs.puts[0][3] == gfs.puts[1][3]  # the same bytes: sealed once


async def test_a_blob_over_a_gfs_limit_is_not_sent_there(tmp_path):
    _write(tmp_path, "a.webp", 70_000)
    g1 = _Conn("g1", "https://g1.example")
    g2 = _Conn("g2", "https://g2.example")
    gfs = _Gfs([g1, g2], follower=[g2], max_blob={"g1": 9_000_000, "g2": 65_000})
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True)
    )
    assert [p[0] for p in gfs.puts] == ["g1"]


async def test_transient_failures_wait_in_the_durable_queue(tmp_path):
    """A transport error, 408 / 429 or 5xx is ``transient`` — the durable
    queue tries the row again (no attempt limit; a restart loses nothing)."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=[None, 503, 429, 201])
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    outcomes = await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    assert outcomes == ["delivered"]
    assert len(gfs.puts) == 4


async def test_a_refusal_is_not_retried(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    for status in (403, 413, 422):
        gfs = _Gfs([g1], status=status)
        u = _uploader(
            tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
        )
        outcomes = await _upload(
            u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
        )
        assert outcomes == ["permanent"]
        assert len(gfs.puts) == 1
    assert "refused" in caplog.text


async def test_a_file_changed_since_planning_is_not_sent(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    _write(tmp_path, "a.webp", 10)
    await _upload(u, plan)
    assert gfs.puts == []


async def test_a_described_file_is_not_sealed_again(tmp_path, monkeypatch):
    """The member broadcast and the seed holder's follower relay both plan
    the same post: the file is sealed once (a bounded descriptor cache keyed
    by the file's size and mtime)."""
    _write(tmp_path, "a.webp", 1000)
    calls = []
    real = up_mod.seal_file_sync

    def _counting(*args):
        calls.append(args[5])
        return real(*args)

    monkeypatch.setattr(up_mod, "seal_file_sync", _counting)
    u = _uploader(tmp_path, gfs=_Gfs([]))
    first = await u._describe_names(SPACE, ["a.webp"], ITEM)
    second = await u._describe_names(SPACE, ["a.webp"], ITEM)
    assert first == second
    assert calls == ["a.webp"]
    # A changed file is described afresh.
    _write(tmp_path, "a.webp", 1001)
    third = await u._describe_names(SPACE, ["a.webp"], ITEM)
    assert third[0].blob_id != first[0].blob_id
    assert calls == ["a.webp", "a.webp"]


async def test_a_stored_upload_is_logged(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=201)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    with caplog.at_level("INFO"):
        await _upload(u, plan)
    line = next(r.getMessage() for r in caplog.records if "uploaded" in r.getMessage())
    assert "a.webp" in line and plan.refs[0].blob_id[:12] in line and "g1" in line


# ── v_51 private spaces: the channel scope ──────────────────────────────


@dataclass
class _Grant:
    channel_id: str = CHANNEL_ID
    epoch: int = 4
    epoch_offset: int = 1000
    writer_key: object | None = None

    @property
    def channel_epoch(self):
        return self.epoch + self.epoch_offset


class _Channels:
    def __init__(self, grant, targets=()):
        self.grant = grant
        #: v_97 — the GFSs the space's channel lives on.
        self.targets = list(targets)

    async def current_grant(self, space_id):
        return self.grant

    async def plan(self, space_id):
        return self.targets


def _channel_uploader(tmp_path, gfs, *, seed=None, grant=None):
    channel_pk = (
        channel_pk_of(derive_channel_seed(seed, SPACE, CHANNEL_ID)) if seed else "pk"
    )
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        spaces=_Spaces(seed, channel=(CHANNEL_ID, channel_pk), private_gfs=True),
    )
    u.attach_channels(_Channels(grant))
    return u


async def test_a_private_space_with_a_channel_uses_the_channel_scope(tmp_path):
    u = _channel_uploader(tmp_path, _Gfs([]))
    assert await u.scope_of(SPACE) == ("channel", CHANNEL_ID)
    none = _uploader(tmp_path, gfs=_Gfs([]), spaces=_Spaces(b"\1" * 32, channel=None))
    assert await none.scope_of(SPACE) is None


async def test_a_gfs_without_the_channel_scope_is_left_out(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _channel_uploader(tmp_path, _Gfs([g1], scopes=("space",)), seed=b"\3" * 32)
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False) is None
    )


async def test_the_seed_holder_signs_with_the_channel_key(tmp_path):
    _write(tmp_path, "a.webp", 10)
    seed = b"\3" * 32
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _channel_uploader(tmp_path, gfs, seed=seed)
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    assert gfs.put_scopes == ["channel"]
    _c, scope_id, _b, _blob, header = gfs.puts[0]
    assert scope_id == CHANNEL_ID
    auth = SpaceMediaUploadAuth.from_header(header)
    assert auth.kind == AUTH_CHANNEL and auth.sig_suite == CHANNEL_SUITE_ED25519
    assert auth.claim.scope == "channel" and auth.claim.scope_id == CHANNEL_ID
    channel_seed = derive_channel_seed(seed, SPACE, CHANNEL_ID)
    assert verify_ed25519(
        b64url_decode(channel_pk_of(channel_seed)),
        auth.claim.channel_signing_bytes(),
        b64url_decode(auth.sig),
    )
    assert auth.claim.channel_signing_bytes().startswith(CHANNEL_MEDIA_PUT_PREFIX)


async def test_a_member_signs_with_its_channel_writer_key(tmp_path):
    _write(tmp_path, "a.webp", 10)
    wk = generate_identity_keypair()
    grant = _Grant(
        writer_key=SimpleNamespace(writer_seed=b64url_encode(wk.private_key))
    )
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _channel_uploader(tmp_path, gfs, grant=grant)
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    auth = SpaceMediaUploadAuth.from_header(gfs.puts[0][4])
    assert auth.kind == AUTH_WRITER_KEY
    assert auth.epoch == grant.channel_epoch  # the shifted epoch, never the content one
    assert verify_ed25519(
        wk.public_key,
        auth.claim.writer_signing_bytes(
            kind=AUTH_WRITER_KEY, epoch=grant.channel_epoch
        ),
        b64url_decode(auth.sig),
    )


async def test_no_channel_material_uploads_nothing(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _channel_uploader(tmp_path, gfs, grant=_Grant(channel_id="d" * 32))
    await _upload(
        u, await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    )
    assert gfs.puts == []


def auth_claim_addr_suite(header: str) -> str:
    return SpaceMediaUploadAuth.from_header(header).claim.addr_suite


async def test_a_channel_blob_is_addressed_in_the_channel_scope(tmp_path):
    """The descriptor of a private space's file names the CHANNEL scope; its
    address binds the channel id (which the descriptor never names)."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _channel_uploader(tmp_path, _Gfs([g1]), seed=b"\3" * 32)
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    (ref,) = plan.refs
    assert ref.scope == "channel"
    assert CHANNEL_ID not in str(plan.wire())
    blob = up_mod.seal_file_sync(
        MEDIA_KEY, SPACE, 4, ITEM, OWN, "a.webp", (tmp_path / "a.webp").read_bytes()
    )
    assert ref.blob_id == space_media_address("channel", CHANNEL_ID, blob)


# ── Only this space's own files are ever sealed (#1309 review D) ─────────


async def test_a_file_not_confined_to_the_space_is_never_described(tmp_path):
    """A member may name another space's, a private album's or a household
    file in a post (or record) of S; it is never sealed under S's key."""
    _write(tmp_path, "mine.webp", 10)
    _write(tmp_path, "private.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    refs = _MediaRefs(foreign={"private.webp"})
    u = _uploader(
        tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V}, media_refs=refs
    )
    plan = await u.plan(
        SPACE,
        ["api/media/mine.webp", "api/media/private.webp"],
        item_id=ITEM,
        followers=False,
    )
    assert set(plan.wire()) == {"mine.webp"}
    assert ("private.webp", SPACE) in refs.asked
    # The catch-up path takes the same check.
    plan = await u.plan_for_reader(SPACE, ["api/media/private.webp"], "e", item_id=ITEM)
    assert plan is None


async def test_a_reserved_name_is_never_described(tmp_path):
    _write(tmp_path, "gm-moment.webp", 10)
    _write(tmp_path, "dm-chat.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    assert (
        await u.plan(
            SPACE,
            ["api/media/gm-moment.webp", "api/media/dm-chat.webp"],
            item_id=ITEM,
            followers=False,
        )
        is None
    )


async def test_a_file_that_left_the_space_is_not_uploaded(tmp_path, caplog):
    """Checked again at upload time: a file named outside the space since
    it was described is not sent."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    refs = _MediaRefs()
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        seed=b"\2" * 32,
        media_refs=refs,
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    refs.foreign.add("a.webp")
    await _upload(u, plan)
    assert gfs.puts == []


async def test_the_uploader_has_no_public_describe():
    assert not hasattr(SpaceMediaGfsUploader, "describe")


async def test_a_stored_blob_is_uploaded_again_once_the_gfs_dropped_it(
    tmp_path, monkeypatch
):
    """#1309 review F: the "already stored" memory used to last forever
    while the GFS keeps a blob at most its TTL (3 days by default) — an
    expired blob was never sent again. Each entry now expires with
    min(KEEP_S, the server's TTL)."""
    clock = [1000.0]
    monkeypatch.setattr(up_mod, "_monotonic", lambda: clock[0])
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], ttl_s=600)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, plan)
    await _upload(u, plan)
    assert len(gfs.puts) == 1  # remembered while the GFS still holds it
    clock[0] += 600
    await _upload(u, plan)
    assert len(gfs.puts) == 2  # the GFS may have dropped it: sent again


async def test_no_authorization_is_not_a_gfs_refusal_and_is_logged_once(
    tmp_path, caplog
):
    """#1309 review K: an upload this household can't authorize used to be
    reported as an HTTP 403 from the GFS (and warned per file). It is its
    own outcome now, warned once per space, and never sent."""
    _write(tmp_path, "a.webp", 10)
    _write(tmp_path, "b.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V})
    plan = await u.plan(
        SPACE, ["api/media/a.webp", "api/media/b.webp"], item_id=ITEM, followers=False
    )
    # The authorization went between plan and upload (the seed was dropped).
    u._spaces.seed = None
    with caplog.at_level("WARNING"):
        await _upload(u, plan)
    assert gfs.puts == []
    lines = [r.getMessage() for r in caplog.records]
    assert sum("no upload authorization" in m for m in lines) == 1
    assert not any("HTTP 403" in m for m in lines)


# ── #1309 re-review 1: a blob is sealed for its item and this household ──


async def test_a_plan_seals_for_the_item_and_this_household(tmp_path):
    """The AAD names the item and the household that seals it — the one the
    reader authenticates as delivering the item — so a blob opens only for
    that item from this household, and the same file in two items is two
    blobs."""
    data = _write(tmp_path, "a.webp", 1000)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    (ref,) = plan.refs
    assert (ref.item_id, ref.origin) == (ITEM, OWN)
    blob = up_mod.seal_file_sync(MEDIA_KEY, SPACE, 4, ITEM, OWN, "a.webp", data)
    assert ref.blob_id == space_media_address("channel", CHANNEL_ID, blob)
    other = await u.plan(SPACE, ["api/media/a.webp"], item_id="post-2", followers=False)
    assert other.refs[0].blob_id != ref.blob_id


async def test_no_plan_before_this_household_knows_its_own_id(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    u._federation = None
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=True) is None
    )


async def test_can_upload_answers_with_the_authorization_a_put_would_use(tmp_path):
    """v_82: a link-joined proposer describes a page's files only when it
    holds an upload authorization — the same one a PUT would sign with: the
    channel key of a seed holder (or a channel grant's writer key). A space
    with no channel here holds none (v_96: no space scope)."""
    gfs = _Gfs([])
    assert await _uploader(tmp_path, gfs=gfs, seed=b"\x02" * 32).can_upload(SPACE)
    assert not await _uploader(tmp_path, gfs=gfs, seed=None).can_upload(SPACE)
    no_channel = _uploader(
        tmp_path, gfs=gfs, spaces=_Spaces(b"\x02" * 32, channel=None)
    )
    assert not await no_channel.can_upload(SPACE)


# ── On demand (v_83): the holder re-uses the upload authorization ────────


async def test_authorize_is_the_channel_upload_authorization(tmp_path):
    """The holder side re-uses it for a re-upload on demand; the retired
    ``space`` scope gets none (v_96)."""
    kp = generate_identity_keypair()
    u = _uploader(tmp_path, gfs=_Gfs([]), seed=kp.private_key)
    claim = up_mod.SpaceMediaClaim(
        scope="channel",
        scope_id=CHANNEL_ID,
        blob_id="a" * 64,
        size=10,
        expires_at=9,
        ts=1,
    )
    auth = await u.authorize(claim, SPACE)
    assert auth is not None and auth.kind == AUTH_CHANNEL
    assert (
        await _uploader(tmp_path, gfs=_Gfs([]), seed=None).authorize(claim, SPACE)
        is None
    )
    space_claim = SimpleNamespace(scope="space", scope_id=SPACE)
    assert await u.authorize(space_claim, SPACE) is None


async def test_remember_stored_skips_a_later_eager_upload(tmp_path):
    _write(tmp_path, "a.webp", 5000)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        seed=generate_identity_keypair().private_key,
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await u.remember_stored(g1, plan.refs[0].blob_id)
    await _upload(u, plan)
    assert gfs.puts == []


# ── v_87: a GFS without a media cache pushes through the relay ───────────


class _NoCacheGfs(_Gfs):
    def __init__(self, conns, *, off: bool) -> None:
        super().__init__(conns)
        self.off = off

    def space_media_cache_off(self, conn) -> bool:
        return self.off


@pytest.mark.parametrize(
    ("version", "off", "reader"),
    [
        (FederationCapability.MIN_FOR_RELAY_MEDIA, True, False),  # push via relay
        (FederationCapability.MIN_FOR_RELAY_MEDIA, False, True),  # cache
        (FederationCapability.MIN_FOR_RELAY_MEDIA - 1, True, True),  # older reader
    ],
)
async def test_a_reader_without_a_cache_gets_the_relay_push(
    tmp_path, version, off, reader
):
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(
        tmp_path,
        gfs=_NoCacheGfs([g1], off=off),
        instances=[RELAY],
        versions={"e": version},
    )
    assert await u.is_relay_reader("e") is reader


async def test_a_reader_whose_gfs_we_are_not_connected_to_stays_a_reader(tmp_path):
    u = _uploader(
        tmp_path,
        gfs=_NoCacheGfs([], off=True),
        instances=[RELAY],
        versions={"e": FederationCapability.MIN_FOR_RELAY_MEDIA},
    )
    assert await u.is_relay_reader("e") is True


async def test_a_reader_reads_a_file_from_its_gfs_only_once_it_is_stored_there(
    tmp_path,
):
    """Round 8: the push to a link-joined reader is dropped only when its
    GFS is known to hold the file (uploaded there). A reader whose GFS never
    got the file keeps its push, whatever its version."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=200)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    assert await u.reads_from_gfs("e", "a.webp") is False  # not stored yet
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, plan)
    assert await u.reads_from_gfs("e", "a.webp") is True
    assert await u.reads_from_gfs("e", "b.webp") is False
    assert await u.reads_from_gfs("b", "a.webp") is False  # no reader
    for version in (V, OURS):
        fresh = _uploader(tmp_path, gfs=gfs, instances=[RELAY], versions={"e": version})
        assert await fresh.reads_from_gfs("e", "a.webp") is False, version


async def test_stored_on_gfs_keeps_only_blobs_the_gfs_holds(tmp_path):
    """The v_92 catch-up describes only the exact blobs the reader's GFS is
    known to hold — matched by address, never by file name: after a key
    rotation the same file is a different blob, which the GFS lacks."""
    _write(tmp_path, "a.webp", 10)
    _write(tmp_path, "b.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=200)
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY, PAIRED],
        versions={"e": V},
        seed=b"\2" * 32,
    )
    urls = ["api/media/a.webp", "api/media/b.webp"]
    assert u.stored_on_gfs(None) is None
    plan = await u.plan_for_reader(SPACE, urls, "e", item_id=ITEM)
    assert u.stored_on_gfs(plan) is None  # nothing uploaded yet
    stored = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, stored)
    narrowed = u.stored_on_gfs(await u.plan_for_reader(SPACE, urls, "e", item_id=ITEM))
    assert set(narrowed.wire()) == {"a.webp"}

    # Rotated epoch: same file, new address — not on the GFS.
    async def _rotated(space_id, *, epoch=None):
        return 5, os.urandom(32)

    real_key = u._crypto.media_key
    u._crypto.media_key = _rotated
    rotated = await u.plan_for_reader(SPACE, urls, "e", item_id=ITEM)
    assert rotated is not None and {r.epoch for r in rotated.refs} == {5}
    assert u.stored_on_gfs(rotated) is None
    u._crypto.media_key = real_key
    # Another item showing the same file is another blob as well.
    other = await u.plan_for_reader(SPACE, urls, "e", item_id="post-2")
    assert u.stored_on_gfs(other) is None


async def test_stored_plan_for_reader_seals_nothing(tmp_path):
    """Review 9: the narrow v_92 lookup never seals — a file with no kept
    description cannot be on the GFS; one uploaded there is described."""
    _write(tmp_path, "a.webp", 10)
    _write(tmp_path, "b.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=200)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    urls = ["api/media/a.webp", "api/media/b.webp"]
    assert await u.stored_plan_for_reader(SPACE, urls, "e", item_id=ITEM) is None
    assert len(u._described) == 0  # nothing sealed
    stored = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    await _upload(u, stored)
    sealed = len(u._described)
    plan = await u.stored_plan_for_reader(SPACE, urls, "e", item_id=ITEM)
    assert set(plan.wire()) == {"a.webp"}
    assert len(u._described) == sealed  # b.webp still not sealed
    assert await u.stored_plan_for_reader(SPACE, urls, "b", item_id=ITEM) is None


async def test_a_global_space_with_a_channel_uses_the_channel_scope(tmp_path):
    """v_96 — a global space running a channel uploads under it: its
    space-keyed writer-key pin is no longer announced once it does."""
    on = _uploader(tmp_path, gfs=_Gfs([]), spaces=_Spaces(channel=(CHANNEL_ID, "pk")))
    assert await on.scope_of(SPACE) == ("channel", CHANNEL_ID)
    off = _uploader(tmp_path, gfs=_Gfs([]), spaces=_Spaces(channel=None))
    assert await off.scope_of(SPACE) is None


async def test_a_plan_reads_the_space_scope_once(tmp_path, monkeypatch):
    """P9 (P7 review): a plan read the space and its channel up to four
    times (plan, _plan, can_upload, _describe_names); once is enough."""
    _write(tmp_path, "a.webp", 1000)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    calls = 0
    real = type(u).scope_of

    async def counted(self, space_id):
        nonlocal calls
        calls += 1
        return await real(self, space_id)

    monkeypatch.setattr(type(u), "scope_of", counted)
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    assert plan is not None
    assert calls == 1


# ── v_97: every household of a GFS space, and files in pieces ───────────


async def test_a_gfs_spaces_channel_servers_get_the_files_without_any_reader(
    tmp_path,
):
    """Owner 2026-11-27: every household of a GFS space (paired, mesh,
    link-joined, member or follower) takes its files from the GFS cache, so
    the channel's servers get them even with no link-joined member."""
    _write(tmp_path, "a.webp", 10)
    seed = b"\3" * 32
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        spaces=_Spaces(
            seed,
            channel=(
                CHANNEL_ID,
                channel_pk_of(derive_channel_seed(seed, SPACE, CHANNEL_ID)),
            ),
            private_gfs=True,
        ),
    )
    u.attach_channels(_Channels(None, targets=[g1]))
    plan = await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False)
    assert [t.id for t in plan.targets] == ["g1"]
    assert await _upload(u, plan) == ["delivered"]


BIG = 2 * PIECE_BYTES + 12_345


def _pieced_uploader(tmp_path, gfs, seed=b"\3" * 32):
    g1 = next(iter(gfs.conns.values()))
    u = _uploader(
        tmp_path,
        gfs=gfs,
        spaces=_Spaces(
            seed,
            channel=(
                CHANNEL_ID,
                channel_pk_of(derive_channel_seed(seed, SPACE, CHANNEL_ID)),
            ),
            private_gfs=True,
        ),
    )
    u.attach_channels(_Channels(None, targets=[g1]))
    return u


async def test_a_file_over_one_blob_goes_in_pieces_with_one_descriptor(tmp_path):
    data = _write(tmp_path, "v.webm", BIG)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _pieced_uploader(tmp_path, gfs)
    plan = await u.plan(SPACE, ["api/media/v.webm"], item_id=ITEM, followers=False)
    [entry] = plan.wire().values()
    # One entry: the file's address, its size and checksum — no piece list.
    assert entry["addr_suite"] == "sm1p"
    assert entry["size"] == BIG
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert "pieces" not in entry and "manifest" not in entry
    # One durable row per piece per GFS; each PUT proves its piece (the
    # fake GFS runs the GFS's own check).
    assert await _upload(u, plan) == ["delivered"] * 3
    assert len(gfs.puts) == 3
    joined = b""
    for index, (_c, _sid, _bid, blob, _hdr) in enumerate(gfs.puts):
        joined += open_space_media(
            blob=blob,
            media_key=MEDIA_KEY,
            space_id=SPACE,
            epoch=4,
            item_id=ITEM,
            origin=OWN,
            filename=f"v.webm#{index}/3",
        )
    assert joined == data


async def test_a_big_file_with_no_gfs_keeping_pieces_is_told_to_its_author(
    tmp_path,
):
    _write(tmp_path, "v.webm", BIG)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], pieces=False)
    u = _pieced_uploader(tmp_path, gfs)
    box = _Outbox()
    u.attach_outbox(box)
    plan = await u.plan(
        SPACE,
        ["api/media/v.webm"],
        item_id=ITEM,
        followers=False,
        author_user_id="anna",
    )
    assert plan is None
    assert box.failed == [(SPACE, "anna", "too_large")]


async def test_a_piece_whose_file_changed_is_not_sent(tmp_path):
    _write(tmp_path, "v.webm", BIG)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _pieced_uploader(tmp_path, gfs)
    plan = await u.plan(SPACE, ["api/media/v.webm"], item_id=ITEM, followers=False)
    _write(tmp_path, "v.webm", BIG)
    assert await _upload(u, plan) == ["delivered"] * 3  # done: nothing to send
    assert gfs.puts == []


async def test_a_gfs_spaces_files_are_pushed_only_below_v97(tmp_path):
    """v_97 (owner 2026-11-27) — a GFS space's files reach a v_97
    household only through the GFS cache: whatever the file, only older
    households keep the push (everyone while the legacy window is open); a
    mesh space's files go to everyone."""
    g1 = _Conn("g1", "https://g1.example")
    u = _pieced_uploader(tmp_path, _Gfs([g1]))
    u.attach_federation(_Fed({"new": OURS, "old": 90}))
    targets = ["new", "old"]
    assert await u.push_targets(SPACE, "a.webp", targets) == ["old"]
    assert await u.push_targets(SPACE, "any.webp", targets) == ["old"]
    mesh = _uploader(tmp_path, gfs=_Gfs([]), spaces=_Spaces(b"\1" * 32, channel=None))
    mesh.attach_federation(_Fed({"new": OURS}))
    assert await mesh.push_targets(SPACE, "a.webp", targets) == targets

    async def window_open(space_id):
        return True

    u.attach_legacy_window(window_open)
    assert await u.push_targets(SPACE, "a.webp", targets) == targets


async def test_a_v96_host_or_no_stored_channel_keeps_the_push(tmp_path):
    """v_97 (owner B-6) — the cache-only rule is a version and channel
    condition: a space whose host is below v_97 (its own files carry no
    descriptor), or one with no stored channel here yet, keeps the push for
    every household; with a v_97 host a v_97 household gets none."""
    g1 = _Conn("g1", "https://g1.example")
    seed = b"\3" * 32
    channel = (CHANNEL_ID, channel_pk_of(derive_channel_seed(seed, SPACE, CHANNEL_ID)))
    for host, expected in (("host96", ["new"]), ("host97", [])):
        u = _uploader(
            tmp_path,
            gfs=_Gfs([g1]),
            spaces=_Spaces(seed, channel=channel, private_gfs=True, owner=host),
        )
        u.attach_federation(_Fed({"new": OURS, "host96": 96, "host97": OURS}))
        assert await u.push_targets(SPACE, "a.webp", ["new"]) == expected
        assert await u.cache_only(SPACE, "new") is (not expected)
    no_channel = _uploader(
        tmp_path,
        gfs=_Gfs([g1]),
        spaces=_Spaces(seed, channel=None, private_gfs=True, owner="host97"),
    )
    no_channel.attach_federation(_Fed({"new": OURS, "host97": OURS}))
    assert await no_channel.push_targets(SPACE, "a.webp", ["new"]) == ["new"]


async def test_a_catch_up_reader_of_a_gfs_space_is_described_for_the_cache(
    tmp_path,
):
    """v_97 (owner B-8) — a paired (or mesh) v_97 household catching a GFS
    space up gets a LAZY descriptor: every file described by its address
    from our copy, nothing uploaded (no bulk re-upload on a join) — it
    fetches on view, a miss on demand from a holder (v_83)."""
    a = _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _pieced_uploader(tmp_path, gfs)
    u.attach_federation(_Fed({"paired": OURS, "old": 90}))
    box = _Outbox()
    u.attach_outbox(box)
    assert await u.reads_space_from_cache(SPACE, "paired") is True
    assert await u.reads_space_from_cache(SPACE, "old") is False
    plan = await u.plan_for_reader(SPACE, ["api/media/a.webp"], "paired", item_id=ITEM)
    assert plan is not None and plan.lazy and plan.targets == ()
    blob = up_mod.seal_file_sync(MEDIA_KEY, SPACE, 4, ITEM, OWN, "a.webp", a)
    assert plan.refs[0].size == len(blob)
    await u.upload(plan)
    assert box.rows == [] and gfs.puts == []
    assert (
        await u.plan_for_reader(SPACE, ["api/media/a.webp"], "old", item_id=ITEM)
        is None
    )


async def test_a_file_no_gfs_of_the_space_takes_is_told_to_its_author(tmp_path):
    """v_97 — with no GFS of the space keeping its media, a GFS space's
    file has no way to its households: the author is told (no push)."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _pieced_uploader(tmp_path, _Gfs([g1], scopes=()))
    box = _Outbox()
    u.attach_outbox(box)
    plan = await u.plan(
        SPACE,
        ["api/media/a.webp"],
        item_id=ITEM,
        followers=False,
        author_user_id="anna",
    )
    assert plan is None
    assert box.failed == [(SPACE, "anna", "refused")]


async def test_a_file_over_half_a_channels_share_is_never_split(tmp_path):
    """v_97 — a GFS keeps one file in pieces up to half its signed channel
    share: a larger one is not described (its author is told), never half
    uploaded."""
    _write(tmp_path, "v.webm", BIG)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _pieced_uploader(tmp_path, gfs)

    async def small_share(conn):
        return BIG - 1

    gfs.space_media_file_max = small_share
    box = _Outbox()
    u.attach_outbox(box)
    plan = await u.plan(
        SPACE,
        ["api/media/v.webm"],
        item_id=ITEM,
        followers=False,
        author_user_id="anna",
    )
    assert plan is None
    assert box.failed == [(SPACE, "anna", "too_large")]


async def test_an_unreachable_gfs_is_no_verdict_and_tells_nobody(tmp_path):
    """Review F (findings 4/8): a GFS whose capability is merely unknown
    right now, or a catch-up plan (no author), never shows "Couldn't
    send". The unreachable GFS's file is still described and its row waits
    in the durable queue (owner B-7); the attempt is transient, not gone."""
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], scopes=())
    gfs.unknown = True
    u = _pieced_uploader(tmp_path, gfs)
    box = _Outbox()
    u.attach_outbox(box)
    plan = await u.plan(
        SPACE,
        ["api/media/a.webp"],
        item_id=ITEM,
        followers=False,
        author_user_id="anna",
    )
    assert plan is not None and [t.id for t in plan.targets] == ["g1"]
    await u.upload(plan, author_user_id="anna")
    assert [r.gfs_id for r in box.rows] == ["g1"]
    assert (await u.deliver(box.rows[0])).kind == "transient"
    gfs.unknown = False
    assert (
        await u.plan(SPACE, ["api/media/a.webp"], item_id=ITEM, followers=False) is None
    )
    assert box.failed == []


async def test_lazy_describing_is_capped_per_household(tmp_path, monkeypatch):
    """v_97 — at most ``MAX_DESCRIBING_STREAMS`` lazy catch-up describes
    seal at once on a household; the rest wait their turn."""
    _write(tmp_path, "a.webp", 10)
    gfs = _Gfs([_Conn("g1", "https://g1.example")])
    u = _pieced_uploader(tmp_path, gfs)
    u.attach_federation(_Fed({"paired": OURS}))
    running = 0
    peak = 0
    lock = threading.Lock()
    real = up_mod.seal_and_address_sync

    def slow(*args):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return real(*args)

    # Only the sealing (in a worker thread) takes a slot.
    monkeypatch.setattr(up_mod, "seal_and_address_sync", slow)
    plans = await asyncio.gather(
        *(
            u.plan_for_reader(SPACE, ["api/media/a.webp"], "paired", item_id=f"i-{i}")
            for i in range(6)
        )
    )
    assert all(p is not None and p.lazy for p in plans)
    assert peak == up_mod.MAX_DESCRIBING_STREAMS


async def test_a_page_version_with_open_sides_is_described_whole(tmp_path):
    """v_97 — one item describes up to the largest descriptor any item
    carries (a page version and its open sides), not only 16 files."""
    names = [f"f{i}.webp" for i in range(40)]
    for name in names:
        _write(tmp_path, name, 10)
    gfs = _Gfs([_Conn("g1", "https://g1.example")])
    u = _pieced_uploader(tmp_path, gfs)
    u.attach_federation(_Fed({"paired": OURS}))
    plan = await u.plan_for_reader(
        SPACE, [f"api/media/{n}" for n in names], "paired", item_id=ITEM
    )
    assert plan is not None and len(plan.refs) == 40


class _DescribeFetcher:
    """Fetches a file held only as a GFS descriptor: ``arrive`` lands it,
    anything else stays on its way (``running``)."""

    def __init__(self, media_dir, arrive=(), running=()):
        self.dir = media_dir
        self.arrive = set(arrive)
        self.running = set(running)
        self.asked: list[str] = []
        self.at_once = 0
        self.peak = 0

    async def ensure_local(self, filename, *, timeout_s):
        self.asked.append(filename)
        self.at_once += 1
        self.peak = max(self.peak, self.at_once)
        await asyncio.sleep(0.01)
        self.at_once -= 1
        if filename in self.arrive:
            (self.dir / filename).write_bytes(b"x" * 10)
            return True
        return False

    def fetching(self, filename):
        return filename in self.running


async def test_a_file_held_only_as_a_descriptor_is_fetched_before_it_is_described(
    tmp_path,
):
    """v_97 (owner: no loss) — a household republishing a file it holds only
    as a GFS descriptor (a video nobody viewed here) fetches it first, then
    describes it; a file whose fetch is still running is ``pending`` in a
    lazy plan (a catch-up holds its record back), one no GFS holds is
    neither."""
    gfs = _Gfs([_Conn("g1", "https://g1.example")])
    u = _pieced_uploader(tmp_path, gfs)
    u.attach_federation(_Fed({"paired": OURS}))
    fetcher = _DescribeFetcher(tmp_path, arrive={"v.webm"}, running={"slow.webm"})
    u.attach_fetcher(fetcher)
    plan = await u.plan(SPACE, ["api/media/v.webm"], item_id=ITEM, followers=False)
    assert plan is not None and [r.filename for r in plan.refs] == ["v.webm"]
    lazy = await u.plan_for_reader(
        SPACE,
        ["api/media/slow.webm", "api/media/gone.webm"],
        "paired",
        item_id=ITEM,
    )
    assert lazy is not None and lazy.refs == () and lazy.pending == ("slow.webm",)
    gone = await u.plan_for_reader(
        SPACE, ["api/media/gone.webm"], "paired", item_id=ITEM
    )
    assert gone is None


async def test_fetches_to_describe_are_capped_per_space(tmp_path):
    """One republish can't start unbounded downloads: at most
    MAX_DESCRIBE_FETCHES_PER_SPACE fetch-to-describe at once per space."""
    gfs = _Gfs([_Conn("g1", "https://g1.example")])
    u = _pieced_uploader(tmp_path, gfs)
    fetcher = _DescribeFetcher(tmp_path)
    u.attach_fetcher(fetcher)
    await asyncio.gather(
        *(
            u.plan(SPACE, [f"api/media/m{i}.webm"], item_id=f"i-{i}", followers=False)
            for i in range(6)
        )
    )
    assert len(fetcher.asked) == 6
    assert fetcher.peak == up_mod.MAX_DESCRIBE_FETCHES_PER_SPACE
