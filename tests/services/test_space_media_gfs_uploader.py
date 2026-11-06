"""Seal a space item's files once and upload them once per GFS
(:mod:`socialhome.services.space_media_gfs_uploader`)."""

from __future__ import annotations

import asyncio
import os
import pathlib
from dataclasses import dataclass, field

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_PUT,
    verify_authority_event,
)
from socialhome.crypto import b64url_decode, generate_identity_keypair, verify_ed25519
from socialhome.domain.federation import InstanceSource
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.gfs_space_media import (
    AUTH_AUTHORITY,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    SpaceMediaUploadAuth,
)
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
    def __init__(self, conns, *, max_blob=9_000_000, follower=(), status=201):
        self.conns = {c.inbox_url: c for c in conns}
        self.max_blob = max_blob
        self.follower = list(follower)
        self.status = status
        self.puts: list[tuple[str, str, str, bytes, str, bool]] = []

    async def space_media_max_blob(self, conn):
        if isinstance(self.max_blob, dict):
            return self.max_blob.get(conn.id)
        return self.max_blob

    async def connection_for_url(self, url):
        return self.conns.get(url)

    async def space_media_follower_targets(self, space_id):
        return self.follower

    async def put_space_media(
        self, conn, *, space_id, blob_id, blob, auth_header, identified=False
    ):
        self.puts.append((conn.id, space_id, blob_id, blob, auth_header, identified))
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
    def __init__(self, versions):
        self.versions = versions

    async def peer_supports(self, iid, *, min_version):
        return self.versions.get(iid, 0) >= min_version


class _Crypto:
    async def media_key(self, space_id, *, epoch=None):
        if space_id != SPACE:
            return None
        return (epoch if epoch is not None else 4), MEDIA_KEY

    async def get_current_epoch(self, space_id):
        return 4


class _Spaces:
    def __init__(self, seed=None):
        self.seed = seed

    async def get_space_seed(self, space_id):
        return self.seed


@dataclass
class _Cert:
    scope: str = "write"
    instance_pk: str = ""

    def v1(self):
        return self

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


@dataclass
class _Certs:
    writer_key: bytes | None = None
    cert: object | None = None
    asked: list = field(default_factory=list)

    async def own_writer_key(self, space_id, epoch):
        self.asked.append(("key", epoch))
        return self.writer_key

    async def own_cert(self, space_id, epoch):
        self.asked.append(("cert", epoch))
        return self.cert


def _write(media_dir: pathlib.Path, name: str, size: int) -> bytes:
    data = os.urandom(size)
    (media_dir / name).write_bytes(data)
    return data


def _uploader(
    tmp_path,
    *,
    gfs,
    instances=(),
    versions=None,
    seed=None,
    certs=None,
    identity_seed=b"\x01" * 32,
):
    u = SpaceMediaGfsUploader(
        space_repo=_Spaces(seed),
        space_crypto=_Crypto(),
        federation_repo=_FedRepo(instances),
        gfs_service=gfs,
        media_dir=tmp_path,
        writer_certs=certs,
    )
    u.attach_federation(_Fed(versions or {}))
    u.attach_identity(own_identity_seed=identity_seed)
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


# ── Planning ─────────────────────────────────────────────────────────────


async def test_no_gfs_reader_means_no_plan_and_no_sealing(tmp_path):
    _write(tmp_path, "a.webp", 1000)
    u = _uploader(tmp_path, gfs=_Gfs([]), instances=[PAIRED], versions={"b": V})
    assert await u.plan(SPACE, ["api/media/a.webp"], followers=False) is None


async def test_plan_for_a_relay_seat_describes_each_file(tmp_path):
    a = _write(tmp_path, "a.webp", 1000)
    _write(tmp_path, "b.webp", 70_000)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    plan = await u.plan(
        SPACE,
        ["/api/media/a.webp?sig=x", "api/media/b.webp", "https://evil/x.webp"],
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
    blob = up_mod.seal_file_sync(MEDIA_KEY, SPACE, 4, "a.webp", a)
    assert entry["blob_id"] == blob_id_of(blob) == ref.blob_id
    assert entry["size"] == len(blob)


async def test_followers_target_the_follower_gfs_set(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g2 = _Conn("g2", "https://g2.example")
    u = _uploader(tmp_path, gfs=_Gfs([], follower=[g2]))
    assert await u.plan(SPACE, ["api/media/a.webp"], followers=False) is None
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=True)
    assert [t.id for t in plan.targets] == ["g2"]


async def test_a_gfs_without_the_capability_is_left_out(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], max_blob=None)
    u = _uploader(tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V})
    assert await u.plan(SPACE, ["api/media/a.webp"], followers=False) is None


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
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=True)
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
        followers=False,
    )
    assert set(plan.wire()) == {"ok.webp"}
    # A space we hold no key for: nothing to describe.
    assert await u.describe("other", ["api/media/ok.webp"]) == []


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
    plan = await u.plan_for_reader(SPACE, ["api/media/a.webp"], "e")
    assert [t.id for t in plan.targets] == ["g1"]
    assert await u.plan_for_reader(SPACE, ["api/media/a.webp"], "nobody") is None


# ── Uploading ────────────────────────────────────────────────────────────


async def _upload(u, plan):
    await u.start()
    try:
        u.upload(plan)
        await asyncio.wait_for(u.drain(), timeout=5)
    finally:
        await u.stop()


async def test_seed_holder_uploads_with_the_space_authority(tmp_path):
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
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=False)
    await _upload(u, plan)
    assert len(gfs.puts) == 1
    conn_id, space_id, blob_id, blob, header, identified = gfs.puts[0]
    assert (conn_id, space_id, identified) == ("g1", SPACE, False)
    assert blob_id == blob_id_of(blob) == plan.refs[0].blob_id
    assert (
        open_space_media(
            blob=blob, media_key=MEDIA_KEY, space_id=SPACE, epoch=4, filename="a.webp"
        )
        == data
    )
    auth = SpaceMediaUploadAuth.from_header(header)
    assert auth.kind == AUTH_AUTHORITY
    assert auth.claim.blob_id == blob_id and auth.claim.size == len(blob)
    assert auth.claim.expires_at > auth.claim.ts
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
        space_id=SPACE,
        payload=auth.claim.signing_body(),
        authority_sig=auth.sig,
        authority_sig_suite=auth.sig_suite,
        space_public_key=kp.public_key,
    )


async def test_writer_key_holder_uploads_anonymously(tmp_path):
    _write(tmp_path, "a.webp", 10)
    wk = generate_identity_keypair()
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        certs=_Certs(writer_key=wk.private_key),
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
    _c, _s, _b, _blob, header, identified = gfs.puts[0]
    auth = SpaceMediaUploadAuth.from_header(header)
    assert auth.kind == AUTH_WRITER_KEY and auth.epoch == 4
    assert identified is False
    assert verify_ed25519(
        wk.public_key,
        auth.claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=4),
        b64url_decode(auth.sig),
    )


async def test_writer_cert_holder_uploads_identified(tmp_path):
    _write(tmp_path, "a.webp", 10)
    ident = generate_identity_keypair()
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    cert = _Cert(instance_pk="pk")
    u = _uploader(
        tmp_path,
        gfs=gfs,
        instances=[RELAY],
        versions={"e": V},
        certs=_Certs(cert=cert),
        identity_seed=ident.private_key,
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
    _c, _s, _b, _blob, header, identified = gfs.puts[0]
    assert identified is True
    auth = SpaceMediaUploadAuth.from_header(header)
    assert auth.kind == AUTH_WRITER_CERT
    assert verify_ed25519(
        ident.public_key,
        auth.claim.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=4),
        b64url_decode(auth.sig),
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
        certs=_Certs(cert=_Cert(scope="comment")),
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
    assert gfs.puts == []


async def test_already_stored_is_success_and_a_repeat_is_not_resent(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=200)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=False)
    await u.start()
    try:
        u.upload(plan)
        await asyncio.wait_for(u.drain(), timeout=5)
        u.upload(plan)
        await asyncio.wait_for(u.drain(), timeout=5)
    finally:
        await u.stop()
    assert len(gfs.puts) == 1


async def test_two_gfs_get_one_upload_each(tmp_path):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    g2 = _Conn("g2", "https://g2.example")
    gfs = _Gfs([g1, g2], follower=[g2])
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=True))
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
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=True))
    assert [p[0] for p in gfs.puts] == ["g1"]


async def test_transient_failures_retry_a_bounded_number_of_times(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(up_mod, "RETRY_BASE_S", 0.0)
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=[None, 503, 201])
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
    assert len(gfs.puts) == 3

    gfs = _Gfs([g1], status=503)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
    assert len(gfs.puts) == up_mod.MAX_ATTEMPTS
    assert "gave up" in caplog.text


async def test_a_refusal_is_not_retried(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    for status in (403, 413, 422):
        gfs = _Gfs([g1], status=status)
        u = _uploader(
            tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
        )
        await _upload(u, await u.plan(SPACE, ["api/media/a.webp"], followers=False))
        assert len(gfs.puts) == 1
    assert "refused" in caplog.text


async def test_a_file_changed_since_planning_is_not_sent(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1])
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=False)
    _write(tmp_path, "a.webp", 10)
    await _upload(u, plan)
    assert gfs.puts == []


async def test_a_full_queue_drops_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(up_mod, "MAX_QUEUED", 1)
    _write(tmp_path, "a.webp", 10)
    _write(tmp_path, "b.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    u = _uploader(tmp_path, gfs=_Gfs([g1]), instances=[RELAY], versions={"e": V})
    plan = await u.plan(
        SPACE, ["api/media/a.webp", "api/media/b.webp"], followers=False
    )
    u.upload(plan)
    assert "queue is full" in caplog.text


async def test_start_and_stop_are_idempotent(tmp_path):
    u = _uploader(tmp_path, gfs=_Gfs([]))
    await u.start()
    await u.start()
    await u.stop()
    await u.stop()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(up_mod, "RETRY_BASE_S", 0.0)


async def test_a_described_file_is_not_sealed_again(tmp_path, monkeypatch):
    """The member broadcast and the seed holder's follower relay both plan
    the same post: the file is sealed once (a bounded descriptor cache keyed
    by the file's size and mtime)."""
    _write(tmp_path, "a.webp", 1000)
    calls = []
    real = up_mod.seal_file_sync

    def _counting(*args):
        calls.append(args[3])
        return real(*args)

    monkeypatch.setattr(up_mod, "seal_file_sync", _counting)
    u = _uploader(tmp_path, gfs=_Gfs([]))
    first = await u.describe(SPACE, ["api/media/a.webp"])
    second = await u.describe(SPACE, ["api/media/a.webp"])
    assert first == second
    assert calls == ["a.webp"]
    # A changed file is described afresh.
    _write(tmp_path, "a.webp", 1001)
    third = await u.describe(SPACE, ["api/media/a.webp"])
    assert third[0].blob_id != first[0].blob_id
    assert calls == ["a.webp", "a.webp"]


async def test_a_stored_upload_is_logged(tmp_path, caplog):
    _write(tmp_path, "a.webp", 10)
    g1 = _Conn("g1", "https://g1.example")
    gfs = _Gfs([g1], status=201)
    u = _uploader(
        tmp_path, gfs=gfs, instances=[RELAY], versions={"e": V}, seed=b"\2" * 32
    )
    plan = await u.plan(SPACE, ["api/media/a.webp"], followers=False)
    with caplog.at_level("INFO"):
        await _upload(u, plan)
    line = next(r.getMessage() for r in caplog.records if "uploaded" in r.getMessage())
    assert "a.webp" in line and plan.refs[0].blob_id[:12] in line and "g1" in line
