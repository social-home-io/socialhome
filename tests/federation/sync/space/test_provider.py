"""Unit tests for :class:`SpaceSyncService`."""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import (
    DELIVERY_ERROR_RELAY_THROTTLED,
    DELIVERY_ERROR_ROUTE_COOLDOWN,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.file_hashes import FileHash, space_content_media_name
from socialhome.domain.space import SpaceMember
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space import provider as provider_mod
from socialhome.federation.sync.space.exporter import (
    SENTINEL_RESOURCE,
    ChunkBuilder,
    parse_chunk,
)
from socialhome.federation.sync.space.file_gate import CatchUpFileGates
from socialhome.federation.sync.space.provider import (
    SpaceSyncService,
)
from socialhome.federation.sync.space.window import KEEP_FOREVER


class _FakeExporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")


class _FakeRtc:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_chunk(self, data):
        self.sent.append(data if isinstance(data, bytes) else data.encode())


class _FakeSession:
    def __init__(
        self,
        sync_id="sync-x",
        space_id="sp-1",
        requester="peer-r",
        sync_mode="initial",
        have_seq=None,
    ):
        self.sync_id = sync_id
        self.space_id = space_id
        self.requester_instance_id = requester
        self.sync_mode = sync_mode
        self.have_seq = have_seq
        self.rtc = _FakeRtc()
        self.touches = 0

    def touch(self) -> None:
        self.touches += 1


@pytest.fixture
def encoder():
    return FederationEncoder(generate_identity_keypair().private_key)


@pytest.fixture
def provider(encoder):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "posts": _FakeExporter("posts", [{"id": "p-1", "author": "u-1"}]),
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


async def test_stream_initial_sends_chunks_then_sentinel(provider):
    session = _FakeSession()
    await provider.stream_initial(session)
    # Expect chunks for the two configured exporters + a sentinel.
    assert len(session.rtc.sent) >= 3
    # Parse the last frame — should be the sentinel.
    last = orjson.loads(session.rtc.sent[-1])
    assert last["resource"] == SENTINEL_RESOURCE
    assert last["is_last"] is True


async def test_stream_initial_skips_missing_exporters(provider):
    """Resources without a registered exporter are skipped (this
    fixture only provides 2 of the 11)."""
    session = _FakeSession()
    await provider.stream_initial(session)
    # Chunks are posts + members + sentinel = 3 frames.
    assert len(session.rtc.sent) == 3


async def test_stream_request_more_only_sends_that_resource(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "posts"})
    assert len(session.rtc.sent) == 1
    parsed = orjson.loads(session.rtc.sent[0])
    assert parsed["resource"] == "posts"


async def test_stream_request_more_unknown_resource_is_noop(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "not_real"})
    assert session.rtc.sent == []


# ─── Part C: HTTPS-mode transport ─────────────────────────────────────


async def test_stream_initial_uses_https_when_session_marked(provider):
    """When ``session.transport_mode == "https"`` (the WebRTC handshake
    couldn't finish — carrier-grade NAT, missing STUN — and the
    requester re-issued the BEGIN with ``prefer_direct=False``), the
    provider MUST ship chunks as ``SPACE_SYNC_CHUNK`` federation
    events through the attached federation service instead of trying
    the absent DataChannel. Before this fix relay-mode sync accepted
    the session and then did nothing.

    The send rides ``send_with_mesh_fallback`` (not bare ``send_event``)
    so a mesh-only requester — a member that joined over a MESH route
    and isn't a confirmed direct peer — receives the chunks via
    ``SPACE_ROUTED``; a confirmed peer still gets the direct path
    internally."""
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    provider.attach_federation(federation)

    await provider.stream_initial(session)

    # Every chunk + the sentinel travels via the mesh-fallback path.
    assert federation.send_with_mesh_fallback.await_count >= 3
    first_call = federation.send_with_mesh_fallback.await_args_list[0]
    assert first_call.kwargs["event_type"].value == "space_sync_chunk"
    assert first_call.kwargs["to_instance_id"] == session.requester_instance_id
    assert first_call.kwargs["space_id"] == session.space_id
    assert first_call.kwargs["payload"]["sync_id"] == session.sync_id
    assert first_call.kwargs["payload"]["chunk"]  # serialised chunk body


async def test_stream_initial_https_chunk_payload_is_json_serialisable(provider):
    """A mesh-routed chunk MUST survive JSON encoding.

    ``send_with_mesh_fallback`` seals the inner payload for the target
    household, and that sealing JSON-encodes it — so a payload holding
    raw ``bytes`` cannot be shipped over the mesh at all. The provider
    used to pass ``serialise_chunk()`` straight through, which returns
    ``bytes``: every chunk to a mesh-only member died with "Object of
    type bytes is not JSON serializable", the failure was swallowed
    (``_send`` ignores the send result), and the member ended up with a
    space, a content key and media bytes but zero post rows.

    The direct path tolerates bytes, which is why this only ever broke
    the relayed topology. ``parse_chunk`` accepts ``bytes | str``, so
    shipping UTF-8 text is lossless for the receiver.
    """
    import json
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    provider.attach_federation(federation)

    await provider.stream_initial(session)

    assert federation.send_with_mesh_fallback.await_count >= 3
    for call in federation.send_with_mesh_fallback.await_args_list:
        payload = call.kwargs["payload"]
        # The actual failure mode: json.dumps raises TypeError on bytes.
        json.dumps(payload)
        # And the ferried body must still parse back into a chunk.
        assert parse_chunk(payload["chunk"])["resource"]


async def test_stream_initial_https_requires_attached_federation(provider):
    """HTTPS-mode without ``attach_federation`` is a wiring bug; the
    provider raises so the missing wiring shows up in tests instead
    of silently dropping chunks in production."""
    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"
    with pytest.raises(RuntimeError, match="attach_federation"):
        await provider._send(session, {"resource": "posts"})


# ─── Catch-up media (#PR442) ──────────────────────────────────────────


async def test_each_shipped_chunk_marks_the_session_active(provider):
    """A long stream is alive while chunks flow — the stale reaper reads
    that, not the session's age."""
    session = _FakeSession()
    await provider.stream_initial(session)
    # Two resource chunks (posts, members); the sentinel is not progress.
    assert session.touches == 2


class _ItemFiles:
    """In-memory ``recent_item_files``: ``(created_at, item, owner, names)``
    rows, served newest first as the repo does."""

    def __init__(self, rows: list[tuple[str, str, str | None, list[str]]]) -> None:
        self.rows = rows
        self.asked: list[tuple[str, str, int]] = []

    async def recent_item_files(self, space_id, *, cutoff, limit):
        self.asked.append((space_id, cutoff, limit))
        return sorted(self.rows, key=lambda r: r[0], reverse=True)


def _catchup_provider(encoder, rows, **kw) -> tuple[SpaceSyncService, AsyncMock]:
    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    media_sync.held_files = AsyncMock(side_effect=list)
    svc = SpaceSyncService(
        builder=ChunkBuilder(encoder=encoder, crypto=_FakeCrypto()),
        exporters=kw.pop("exporters", {}),
        sig_suite="ed25519",
        media_sync=media_sync,
        item_files=_ItemFiles(rows),
        **kw,
    )
    return svc, media_sync


def _shipped(media_sync) -> list[tuple[str, list[str]]]:
    return [
        (c.kwargs["correlation_id"], list(c.kwargs["media_urls"]))
        for c in media_sync.enqueue_for_blob.call_args_list
    ]


async def test_an_incremental_resync_ships_no_catchup_media(encoder):
    """The periodic re-sync re-streams rows but never re-enqueues the
    space's media — that is the joiner's catch-up only."""
    svc, media_sync = _catchup_provider(
        encoder, [("2026-11-01 10:00:00", "p-1", "u-1", ["a.webp"])]
    )
    session = _FakeSession(sync_mode="incremental")
    await svc.stream_initial(session)
    media_sync.enqueue_for_blob.assert_not_awaited()
    assert svc._item_files.asked == []
    # An initial (joiner) sync still ships the space's recent media.
    await svc.stream_initial(_FakeSession())
    assert _shipped(media_sync) == [("p-1", ["a.webp"])]
    parsed = orjson.loads(session.rtc.sent[-1])
    assert parsed["resource"] == SENTINEL_RESOURCE


async def test_catchup_never_queues_a_file_this_household_does_not_hold(encoder):
    """The host holds a member's post whose picture never arrived (the
    member posted while the host was down). Its catch-up must not queue
    that file — every attempt failed with "blob source missing" — and the
    file does not use up the stream's file budget either."""
    svc, media_sync = _catchup_provider(
        encoder,
        [
            ("2026-11-03 10:00:00", "p-missing", "u-d", ["sd-lost.webp"]),
            ("2026-11-02 10:00:00", "p-mixed", "u-1", ["here.webp", "gone.webp"]),
            ("2026-11-01 10:00:00", "p-held", "u-1", ["a.webp"]),
        ],
    )
    held = {"here.webp", "a.webp"}
    media_sync.held_files = AsyncMock(
        side_effect=lambda urls: [u for u in urls if u in held]
    )
    with patch.object(provider_mod, "CATCHUP_MEDIA_MAX_FILES", 2):
        await svc.stream_initial(_FakeSession(requester="peer-d"))
    assert _shipped(media_sync) == [("p-mixed", ["here.webp"]), ("p-held", ["a.webp"])]


async def test_stream_initial_enqueues_catchup_media(encoder):
    """After the metadata sentinel the provider enqueues the files of the
    space's recent items — posts, comments, gallery items, listings,
    covers — newest first, each to the requester only."""
    svc, media_sync = _catchup_provider(
        encoder,
        [
            ("2026-11-01 10:00:00", "post-1", "u-1", ["a.webp"]),
            ("2026-11-03 10:00:00", "c-1", "u-2", ["comment.webp"]),
            ("2026-11-02 10:00:00", "g-1", "u-1", ["full.webp", "thumb.webp"]),
        ],
    )
    await svc.stream_initial(_FakeSession(requester="peer-newcomer"))
    assert _shipped(media_sync) == [
        ("c-1", ["comment.webp"]),
        ("g-1", ["full.webp", "thumb.webp"]),
        ("post-1", ["a.webp"]),
    ]
    for c in media_sync.enqueue_for_blob.call_args_list:
        assert c.kwargs["target_instance_ids"] == ["peer-newcomer"]


async def test_catchup_media_is_bounded_to_the_last_30_days(encoder):
    """Only items created in the last :data:`CATCHUP_MEDIA_MAX_AGE_S` are
    candidates (older files arrive on view, ``SPACE_MEDIA_WANTED``)."""
    from datetime import datetime, timedelta

    svc, _media_sync = _catchup_provider(encoder, [])
    await svc.stream_initial(_FakeSession())
    [(space_id, cutoff, limit)] = svc._item_files.asked
    assert space_id == "sp-1"
    assert limit == provider_mod.CATCHUP_MEDIA_MAX_FILES
    expected = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=provider_mod.CATCHUP_MEDIA_MAX_AGE_S
    )
    assert abs(datetime.fromisoformat(cutoff) - expected) < timedelta(minutes=1)


async def test_catchup_media_stops_at_the_file_budget_newest_first(
    encoder, monkeypatch
):
    """At most :data:`CATCHUP_MEDIA_MAX_FILES` files per stream, newest
    first across every kind of item."""
    monkeypatch.setattr(provider_mod, "CATCHUP_MEDIA_MAX_FILES", 3)
    svc, media_sync = _catchup_provider(
        encoder,
        [
            ("2026-11-01 10:00:00", "old", "u-1", ["o.webp"]),
            ("2026-11-05 10:00:00", "g-new", "u-1", ["f.webp", "t.webp"]),
            ("2026-11-04 10:00:00", "ev", "u-1", ["cover.webp", "x.webp"]),
        ],
    )
    await svc.stream_initial(_FakeSession())
    assert _shipped(media_sync) == [
        ("g-new", ["f.webp", "t.webp"]),
        ("ev", ["cover.webp"]),
    ]


async def test_catchup_media_reaches_a_link_joined_requester(encoder):
    """Every member is equal on any path: a member seated from an invite
    link gets the same bounded catch-up pushes (the outbox drops one at
    send time while the reader's GFS cache holds the file)."""
    fed = SimpleNamespace(
        is_space_session_peer=AsyncMock(return_value=True),
        peer_supports=AsyncMock(return_value=True),
        space_member_version=AsyncMock(return_value=92),
        space_member_supports=AsyncMock(return_value=True),
    )
    svc, media_sync = _catchup_provider(
        encoder, [("2026-11-01 10:00:00", "post-1", "u-1", ["a.webp"])]
    )
    svc._federation = fed
    await svc.stream_initial(_FakeSession(requester="peer-e"))
    assert _shipped(media_sync) == [("post-1", ["a.webp"])]


async def test_stream_initial_no_media_sync_wired_is_noop(encoder):
    """Without ``media_sync`` (test stacks), the catch-up step is a
    no-op — metadata sync still finishes."""
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={"posts": _FakeExporter("posts", [])},
        sig_suite="ed25519",
        media_sync=None,
    )
    session = _FakeSession()
    await svc.stream_initial(session)
    # Sentinel still landed.
    parsed = orjson.loads(session.rtc.sent[-1])
    assert parsed["resource"] == SENTINEL_RESOURCE


async def test_a_failed_item_read_ships_no_catchup_media(encoder):
    """A SQLite error reading the candidates is logged, the stream still
    completes (the next catch-up retries)."""
    import sqlite3

    class _Broken:
        async def recent_item_files(self, space_id, *, cutoff, limit):
            raise sqlite3.OperationalError("locked")

    svc, media_sync = _catchup_provider(encoder, [])
    svc._item_files = _Broken()
    session = _FakeSession()
    await svc.stream_initial(session)
    media_sync.enqueue_for_blob.assert_not_awaited()
    assert orjson.loads(session.rtc.sent[-1])["resource"] == SENTINEL_RESOURCE


# ─── Integration: the real Sqlite repos (#PR444) ──────────────────────
#
# Mock-shaped unit tests caught the wire shape but missed that repo
# methods were never real (#443). This wires ``SpaceSyncService`` to the
# REAL media reference repo so a future rename surfaces as a test failure.


async def test_stream_initial_catchup_real_repos(tmp_dir, encoder):
    """Catch-up reads the real repo: a recent post, a comment picture and a
    gallery item ship; a post older than 30 days does not."""
    from datetime import datetime, timedelta

    from socialhome.db.database import AsyncDatabase
    from socialhome.repositories.media_reference_repo import (
        SqliteMediaReferenceRepo,
    )

    def _ago(days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    db = AsyncDatabase(tmp_dir / "catchup.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("host", "u-host", "Host"),
        )
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, "
            "owner_username, identity_public_key) VALUES(?,?,?,?,?)",
            ("sp-int", "Int Space", "inst-host", "host", "00" * 32),
        )
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, image_urls_json,"
            " created_at) VALUES('p-int-1', 'sp-int', 'u-host', 'image', ?, ?)",
            ('["api/media/integration.webp"]', _ago(1)),
        )
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, media_url,"
            " created_at) VALUES('p-old', 'sp-int', 'u-host', 'image',"
            " 'api/media/old.webp', ?)",
            (_ago(40),),
        )
        await db.enqueue(
            "INSERT INTO space_post_comments(id, post_id, author, type, media_url,"
            " created_at) VALUES('c-1', 'p-int-1', 'u-host', 'image',"
            " 'api/media/comment.webp', ?)",
            (_ago(0),),
        )
        await db.enqueue(
            "INSERT INTO gallery_albums(id, space_id, name) VALUES('alb', 'sp-int', 'A')"
        )
        await db.enqueue(
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height, created_at)"
            " VALUES('g-int-1', 'alb', 'u-host', 'photo', 'full.webp',"
            " 'thumb.webp', 1, 1, ?)",
            (_ago(2),),
        )
        media_sync = AsyncMock()
        media_sync.enqueue_for_blob = AsyncMock()
        media_sync.held_files = AsyncMock(side_effect=list)
        svc = SpaceSyncService(
            builder=ChunkBuilder(encoder=encoder, crypto=_FakeCrypto()),
            exporters={},
            sig_suite="ed25519",
            media_sync=media_sync,
            item_files=SqliteMediaReferenceRepo(db),
        )
        await svc.stream_initial(_FakeSession(space_id="sp-int", requester="p"))
        assert _shipped(media_sync) == [
            ("c-1", ["comment.webp"]),
            ("p-int-1", ["integration.webp"]),
            ("g-int-1", ["full.webp", "thumb.webp"]),
        ]
        for c in media_sync.enqueue_for_blob.call_args_list:
            assert c.kwargs["target_instance_ids"] == ["p"]
    finally:
        await db.shutdown()


# ─── #648: don't push into a broken path forever ──────────────────────


async def test_stream_initial_abandons_after_consecutive_chunk_failures(
    provider, caplog
):
    """A stream whose chunks keep failing is abandoned, not completed.

    Each attempt on the mesh path costs a BFS plus a 3-hop signed round,
    so continuing to push a whole space's metadata into a path that is not
    working is pure cost. Recovery is the requester's re-BEGIN.
    """
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=SimpleNamespace(ok=False, error="no_route"),
    )
    # ``close_sync_session`` is sync in production; an AsyncMock attribute
    # would hand back an un-awaited coroutine.
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    # Drop the budget to 1 so the abort provably fires BEFORE the stream
    # would have ended on its own — the fixture emits only ~3 sends, so
    # asserting await_count == MAX_CONSECUTIVE_CHUNK_FAILURES passed even
    # when the abort never triggered.
    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        caplog.at_level(logging.WARNING, logger="socialhome"),
    ):
        await provider.stream_initial(session)

    assert federation.send_with_mesh_fallback.await_count == 1, (
        "kept streaming past the failure budget"
    )
    assert "abandoning stream" in caplog.text


async def test_stream_initial_requester_gone_mid_stream_is_a_warning_not_error(
    provider, caplog
):
    """The requester going offline closes the DataChannel mid-stream.

    That is an ordinary disconnect: each chunk the closed channel refuses
    counts a strike and the stream is abandoned with ONE warning that names
    the requester — never ``stream_initial failed`` at ERROR with a
    ``ConnectionError: DataChannel not open`` traceback (what the
    federation demo's offline steps logged for every killed household)."""

    class _ClosedRtc(_FakeRtc):
        async def send_chunk(self, data):
            raise ConnectionError("DataChannel not open")

    session = _FakeSession(requester="peer-gone")
    session.rtc = _ClosedRtc()
    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        caplog.at_level(logging.INFO, logger="socialhome"),
    ):
        await provider.stream_initial(session)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info]
    [abandon] = [r for r in caplog.records if "abandoning stream" in r.getMessage()]
    assert abandon.levelno == logging.WARNING
    assert "peer-gone" in abandon.getMessage()


async def test_stream_request_more_stops_on_closed_channel(provider):
    """A REQUEST_MORE slice stops at the first chunk a closed channel
    refuses instead of raising out of the slice."""

    class _ClosedRtc(_FakeRtc):
        calls = 0

        async def send_chunk(self, data):
            type(self).calls += 1
            raise ConnectionError("DataChannel not open")

    class _ThreeChunks:
        async def build_chunks(self, **kw):
            for i in range(3):
                yield {"sync_id": "sync-x", "resource": "posts", "seq": i}

    session = _FakeSession()
    session.rtc = _ClosedRtc()
    with patch.object(provider, "_builder", _ThreeChunks()):
        await provider.stream_request_more(session, {"resource": "posts"})
    assert _ClosedRtc.calls == 1


async def test_stream_initial_failure_budget_resets_on_success(provider):
    """An isolated failure doesn't abandon a stream that recovers."""
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    ok = SimpleNamespace(ok=True, error=None)
    bad = SimpleNamespace(ok=False, error="routed_send_failed")

    # Baseline: how many sends a fully-successful stream makes.
    clean = AsyncMock()
    clean.send_with_mesh_fallback = AsyncMock(return_value=ok)
    provider.attach_federation(clean)
    await provider.stream_initial(session)
    expected = clean.send_with_mesh_fallback.await_count
    assert expected > 1

    # Now alternate failure/success so the run never accumulates MAX in a
    # row. The stream must still make every send a clean run makes.
    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        side_effect=([bad, ok] * expected) + [ok] * expected,
    )
    provider.attach_federation(federation)

    second = _FakeSession()
    second.rtc = None
    second.transport_mode = "https"
    await provider.stream_initial(second)

    assert federation.send_with_mesh_fallback.await_count == expected, (
        "an isolated failure aborted a stream that was recovering"
    )


async def test_abandoned_stream_closes_its_session(provider):
    """Giving up on a stream must not leak the session.

    A held session costs the *requester* one of its three active-session
    slots for the 30-minute stale TTL — and a mesh requester recovers by
    re-issuing BEGIN, so a leaked session blocks precisely the retry that
    would fix it. That is what kept `sync-https-fallback` red even once
    the route came back seconds later (#648).
    """
    from unittest.mock import AsyncMock, MagicMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=SimpleNamespace(ok=False, error="no_route"),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    with patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1):
        await provider.stream_initial(session)

    federation.close_sync_session.assert_called_once_with(session.sync_id)


async def test_successful_stream_leaves_the_session_alone(provider):
    """A stream that completes must NOT tear its own session down.

    The requester closes it on the sentinel; closing it here would race
    that and drop late chunks.
    """
    from unittest.mock import AsyncMock, MagicMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=SimpleNamespace(ok=True, error=None),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    await provider.stream_initial(session)

    federation.close_sync_session.assert_not_called()


# ─── Route-discovery cooldown: a race must not abandon the stream ─────


def _cooldown_result(retry_after_s: float = 0.01):
    """A ``DeliveryResult``-shaped failure from the negative-cooldown branch."""
    return SimpleNamespace(
        ok=False,
        error=DELIVERY_ERROR_ROUTE_COOLDOWN,
        retry_after_s=retry_after_s,
    )


async def _baseline_send_count(provider) -> int:
    """How many sends a fully-successful HTTPS stream makes."""
    from unittest.mock import AsyncMock

    session = _FakeSession(sync_id="sync-baseline")
    session.rtc = None
    session.transport_mode = "https"
    clean = AsyncMock()
    clean.send_with_mesh_fallback = AsyncMock(
        return_value=SimpleNamespace(ok=True, error=None, retry_after_s=None),
    )
    provider.attach_federation(clean)
    await provider.stream_initial(session)
    return clean.send_with_mesh_fallback.await_count


async def test_route_cooldown_failure_waits_and_retries_the_same_chunk(provider):
    """A chunk that failed *because discovery is in its negative cooldown*
    must NOT burn a retry strike.

    ``discover_route`` returns ``None`` immediately — without probing — for
    ``ROUTE_NEGATIVE_COOLDOWN_S`` after a failed flood. The chunk loop has no
    delay between chunks, so one missed discovery window used to fail three
    chunks within milliseconds and abandon the entire stream, even though the
    route was seconds from warming up. Wait the cooldown out and retry the
    same chunk instead.
    """
    from unittest.mock import AsyncMock

    expected = await _baseline_send_count(provider)
    assert expected > 1

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    ok = SimpleNamespace(ok=True, error=None, retry_after_s=None)
    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        side_effect=[_cooldown_result()] + [ok] * (expected + 2),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    # Budget of 1: if the cooldown failure counted a strike the stream would
    # be abandoned after the very first chunk.
    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod, "MAX_ROUTE_COOLDOWN_WAIT_S", 0.01),
    ):
        await provider.stream_initial(session)

    federation.close_sync_session.assert_not_called()
    # One extra send: the retried chunk.
    assert federation.send_with_mesh_fallback.await_count == expected + 1


async def test_route_cooldown_waits_are_bounded(provider):
    """A genuinely unreachable peer still terminates the stream.

    Waiting out a cooldown is bounded per stream — otherwise a household
    that is simply gone parks the provider's task forever.
    """
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=_cooldown_result(),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod, "MAX_ROUTE_COOLDOWN_WAITS", 2),
        patch.object(provider_mod, "MAX_ROUTE_COOLDOWN_WAIT_S", 0.01),
    ):
        await provider.stream_initial(session)

    # First attempt + two bounded waits, then the failure counts a strike
    # and the budget of 1 abandons the stream.
    assert federation.send_with_mesh_fallback.await_count == 3
    federation.close_sync_session.assert_called_once_with(session.sync_id)


async def test_non_cooldown_failure_still_counts_a_strike(provider):
    """Only the cooldown reason buys a wait — every other failure keeps
    today's behaviour exactly (fail fast, count a strike, abandon)."""
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=SimpleNamespace(
            ok=False,
            error="routed_send_failed",
            retry_after_s=None,
        ),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    sleep_mock = AsyncMock()
    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod.asyncio, "sleep", sleep_mock),
    ):
        await provider.stream_initial(session)

    assert federation.send_with_mesh_fallback.await_count == 1
    federation.close_sync_session.assert_called_once_with(session.sync_id)
    sleep_mock.assert_not_awaited()


# ─── Relay back-pressure: a 429 is a window, not a broken path ────────


def _relay_throttle_result(retry_after_s: float = 0.01):
    """A ``DeliveryResult``-shaped 429 from the connection-server relay."""
    return SimpleNamespace(
        ok=False,
        error=DELIVERY_ERROR_RELAY_THROTTLED,
        retry_after_s=retry_after_s,
    )


async def test_a_relay_throttle_waits_and_retries_the_same_chunk(provider):
    """A link-joined household is reachable ONLY through the connection
    server's envelope relay, so every catch-up chunk rides a per-minute
    window. Counting a 429 as a chunk failure abandoned the whole
    backfill after three of them — which is what a few hundred chunks
    hitting the window produces in under a second."""
    from unittest.mock import AsyncMock

    expected = await _baseline_send_count(provider)
    assert expected > 1

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    ok = SimpleNamespace(ok=True, error=None, retry_after_s=None)
    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        side_effect=[_relay_throttle_result()] + [ok] * (expected + 2),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod, "MAX_RELAY_THROTTLE_WAIT_S", 0.01),
    ):
        await provider.stream_initial(session)

    federation.close_sync_session.assert_not_called()
    assert federation.send_with_mesh_fallback.await_count == expected + 1


async def test_relay_throttle_waits_are_bounded(provider):
    """A relay that never lets us through still terminates the stream
    rather than parking the provider task forever."""
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        return_value=_relay_throttle_result(),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod, "MAX_RELAY_THROTTLE_WAITS", 2),
        patch.object(provider_mod, "MAX_RELAY_THROTTLE_WAIT_S", 0.01),
    ):
        await provider.stream_initial(session)

    assert federation.send_with_mesh_fallback.await_count == 3
    federation.close_sync_session.assert_called_once_with(session.sync_id)


async def test_the_two_waitable_reasons_keep_separate_budgets(provider):
    """A stream that waits out a route cooldown must not arrive at the
    relay window with its patience already spent — the two conditions
    are unrelated and a backfill routinely meets both."""
    from unittest.mock import AsyncMock

    expected = await _baseline_send_count(provider)

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    ok = SimpleNamespace(ok=True, error=None, retry_after_s=None)
    federation = AsyncMock()
    federation.send_with_mesh_fallback = AsyncMock(
        side_effect=(
            [_cooldown_result(), _relay_throttle_result()] + [ok] * (expected + 2)
        ),
    )
    federation.close_sync_session = MagicMock()
    provider.attach_federation(federation)

    with (
        patch.object(provider_mod, "MAX_CONSECUTIVE_CHUNK_FAILURES", 1),
        patch.object(provider_mod, "MAX_ROUTE_COOLDOWN_WAITS", 1),
        patch.object(provider_mod, "MAX_ROUTE_COOLDOWN_WAIT_S", 0.01),
        patch.object(provider_mod, "MAX_RELAY_THROTTLE_WAITS", 1),
        patch.object(provider_mod, "MAX_RELAY_THROTTLE_WAIT_S", 0.01),
    ):
        await provider.stream_initial(session)

    federation.close_sync_session.assert_not_called()
    assert federation.send_with_mesh_fallback.await_count == expected + 2


# ─── v_41: a pre-moderator requester gets ``member`` for a moderator ────


def _members_provider(encoder):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "members": _FakeExporter(
            "members",
            [
                {"user_id": "u-mod", "role": "moderator"},
                {"user_id": "u-adm", "role": "admin"},
            ],
        ),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


def _member_roles(session) -> dict[str, str]:
    import base64

    roles: dict[str, str] = {}
    for raw in session.rtc.sent:
        env = orjson.loads(raw)
        if env["resource"] != "members":
            continue
        plain = base64.urlsafe_b64decode(env["encrypted_payload"])
        for r in orjson.loads(plain)["records"]:
            roles[r["user_id"]] = r["role"]
    return roles


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE, "moderator"),
        (FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE - 1, "member"),
    ],
)
async def test_members_stream_degrades_moderator_for_a_v40_requester(
    encoder, version, expected
):
    """A v_40 receiver's ``space_members.role`` CHECK rejects ``moderator``
    and the whole members chunk with it — so a requester below v_41 is
    streamed the role it knows that grants no more. The gate reads the
    session's version (``space_member_version``), so a member reached only
    over the mesh — no paired row, ``peer_supports`` False — at v_41 gets
    its moderators as moderators."""
    from unittest.mock import AsyncMock

    svc = _members_provider(encoder)
    federation = AsyncMock()
    federation.peer_supports = AsyncMock(return_value=False)  # mesh-only
    federation.space_member_version = AsyncMock(return_value=version)
    svc.attach_federation(federation)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert _member_roles(session) == {"u-mod": expected, "u-adm": "admin"}
    session2 = _FakeSession()
    await svc.stream_request_more(session2, {"resource": "members"})
    assert _member_roles(session2) == {"u-mod": expected, "u-adm": "admin"}


async def test_members_stream_without_federation_degrades_conservatively(encoder):
    svc = _members_provider(encoder)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert _member_roles(session)["u-mod"] == "member"


# ─── v_55: the space chat streams only to a writer household ────────────


def _chat_provider(encoder):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "chat_messages": _FakeExporter(
            "chat_messages", [{"id": "m-1", "author_user_id": "u-1", "content": "hi"}]
        ),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


def _resources(session) -> list[str]:
    return [
        orjson.loads(raw)["resource"]
        for raw in session.rtc.sent
        if orjson.loads(raw)["resource"] != SENTINEL_RESOURCE
    ]


@pytest.mark.parametrize(
    ("supports", "writer", "streamed"),
    [
        (True, True, True),
        # A household below v_55, or one holding only follower seats, gets
        # no chat — by catch-up any more than live.
        (False, True, False),
        (True, False, False),
    ],
)
async def test_chat_streams_only_to_a_v55_writer_household(
    encoder, supports, writer, streamed
):
    from unittest.mock import AsyncMock

    svc = _chat_provider(encoder)
    federation = AsyncMock()
    federation.space_member_version = AsyncMock(return_value=55 if supports else 54)
    svc.attach_federation(federation)
    asked: list[tuple[str, str]] = []

    async def _gate(space_id: str, instance_id: str) -> bool:
        asked.append((space_id, instance_id))
        return writer

    svc.attach_member_gate(_gate)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert (_resources(session) == ["chat_messages"]) is streamed
    session2 = _FakeSession()
    await svc.stream_request_more(session2, {"resource": "chat_messages"})
    assert (_resources(session2) == ["chat_messages"]) is streamed
    if supports:
        assert asked and asked[0] == ("sp-1", "peer-r")


async def test_chat_never_streams_without_a_gate_or_federation(encoder):
    svc = _chat_provider(encoder)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert _resources(session) == []

    async def _yes(_space_id: str, _instance_id: str) -> bool:
        return True

    svc.attach_member_gate(_yes)
    session2 = _FakeSession()
    await svc.stream_initial(session2)
    assert _resources(session2) == []


def _zones_provider(encoder) -> SpaceSyncService:
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "space_zones": _FakeExporter("space_zones", [{"id": "z-1", "name": "School"}]),
        "space_zones_deleted": _FakeExporter("space_zones_deleted", [{"id": "z-0"}]),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


@pytest.mark.parametrize("member", [True, False])
async def test_zones_stream_only_to_a_member_household(encoder, member):
    """The members' places are member-only data (owner decision 13): a
    follower-only household gets no zones by catch-up, as it gets none
    live."""
    svc = _zones_provider(encoder)

    async def _gate(_space_id: str, _instance_id: str) -> bool:
        return member

    svc.attach_member_gate(_gate)
    session = _FakeSession()
    await svc.stream_initial(session)
    streamed = sorted(set(_resources(session)))
    assert streamed == (["space_zones", "space_zones_deleted"] if member else [])


def _roster_provider(encoder) -> SpaceSyncService:
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "bans": _FakeExporter("bans", [{"user_id": "u-banned"}]),
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
        "member_pictures": _FakeExporter(
            "member_pictures", [{"user_id": "u-1", "picture_hash": "h"}]
        ),
        "posts": _FakeExporter("posts", [{"id": "p-1"}]),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


async def test_a_member_household_streams_only_its_own_peoples_pictures(encoder):
    """A member household's ``space_members`` also holds the host's
    members (a host stream lands there); the receiver keeps only the
    pictures of people seated on the provider (``acts_for``), so a plain
    member household streams only its own people's."""
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={
            "member_pictures": _FakeExporter(
                "member_pictures",
                [
                    {"user_id": "u-ours", "picture_hash": "h1"},
                    {"user_id": "u-hosts", "picture_hash": "h2"},
                ],
            ),
        },
        sig_suite="ed25519",
    )

    async def _no(_space_id: str) -> bool:
        return False

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours"}

    svc.attach_roster_authority(_no, own_members=_ours)
    session = _FakeSession()
    await svc.stream_initial(session)
    users = [r["user_id"] for r in _records(session).get("member_pictures", [])]
    assert users == ["u-ours"]


async def test_a_member_household_streams_its_own_peoples_member_rows(encoder):
    """A picture reset (``picture_hash`` → ``null``) a receiver missed is
    healed only from the member's own household (``_apply_picture_resets``:
    ``acts_for``) — the host's view of another household's member is
    second-hand. So a plain member household keeps streaming its own
    people's ``members`` rows (never the host's, never ``bans``)."""
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={
            "members": _FakeExporter(
                "members",
                [
                    {"user_id": "u-ours", "role": "member", "picture_hash": None},
                    {"user_id": "u-hosts", "role": "owner", "picture_hash": None},
                ],
            ),
            "bans": _FakeExporter("bans", [{"user_id": "u-banned"}]),
        },
        sig_suite="ed25519",
    )

    async def _no(_space_id: str) -> bool:
        return False

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours"}

    svc.attach_roster_authority(_no, own_members=_ours)
    session = _FakeSession()
    await svc.stream_initial(session)
    streamed = _records(session)
    assert [r["user_id"] for r in streamed["members"]] == ["u-ours"]
    assert "bans" not in streamed


@pytest.mark.parametrize("authority", [True, False])
async def test_only_the_roster_authority_streams_members_and_bans(encoder, authority):
    """Every receiver refuses ``members`` and ``bans`` from a household
    that isn't the space's host (or a host-equivalent seed holder) —
    ``receiver._admit`` — so a plain member household no longer streams
    them to every co-member each session. Its own members' pictures and
    its content still go (the receiver takes those from it)."""
    svc = _roster_provider(encoder)

    async def _authority(_space_id: str) -> bool:
        return authority

    svc.attach_roster_authority(_authority)
    session = _FakeSession()
    await svc.stream_initial(session)
    streamed = sorted(set(_resources(session)))
    expected = ["member_pictures", "posts"]
    if authority:
        expected = ["bans", "member_pictures", "members", "posts"]
    assert streamed == expected


async def test_without_an_authority_check_the_roster_streams_as_before(encoder):
    svc = _roster_provider(encoder)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert sorted(set(_resources(session))) == [
        "bans",
        "member_pictures",
        "members",
        "posts",
    ]


async def test_zones_never_stream_without_a_gate(encoder):
    svc = _zones_provider(encoder)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert _resources(session) == []


# ─── §25.6 incremental sessions: change stamps + per-household watermark ──


class _IncExporter(_FakeExporter):
    """A covered resource: streams ``changed`` when asked since a stamp."""

    def __init__(self, resource: str, records: list[dict], changed: list[dict]):
        super().__init__(resource, records)
        self._changed = changed
        self.asked_since: list[int] = []

    async def iter_changed(self, space_id: str, since: int):
        self.asked_since.append(since)
        yield list(self._changed)


class _Marks:
    """A :class:`SyncWatermarks` stand-in recording what the provider asks."""

    def __init__(self, *, snapshot: int = 40, since: int | None = 7) -> None:
        self._snapshot = snapshot
        self._since = since
        self.planned: list[dict] = []
        self.confirmed: list[dict] = []

    async def snapshot(self) -> int:
        return self._snapshot

    async def since_for(self, **kw) -> int | None:
        self.planned.append(kw)
        return self._since

    async def shape_crossed(self, **kw) -> bool:
        return self.crossed

    crossed = False

    async def confirm(self, **kw) -> None:
        self.confirmed.append(kw)


def _inc_provider(encoder, marks, *, windows=None):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    posts = _IncExporter(
        "posts",
        [{"id": "p-1"}, {"id": "p-2"}, {"id": "p-3"}],
        [{"id": "p-2"}],
    )
    exporters = {
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
        "posts": posts,
    }
    svc = SpaceSyncService(
        builder=builder,
        exporters=exporters,
        sig_suite="ed25519",
        windows=windows,
        watermarks=marks,
    )
    return svc, posts


def _records(session) -> dict[str, list[dict]]:

    out: dict[str, list[dict]] = {}
    for raw in session.rtc.sent:
        env = orjson.loads(raw)
        if env["resource"] == SENTINEL_RESOURCE:
            continue
        plain = base64.urlsafe_b64decode(env["encrypted_payload"])
        out.setdefault(env["resource"], []).extend(orjson.loads(plain)["records"])
    return out


@pytest.mark.parametrize(
    ("authority", "anti_entropy"), [(None, True), (True, True), (False, False)]
)
async def test_only_the_roster_authority_asks_for_the_daily_full_pass(
    encoder, authority, anti_entropy
):
    """#2(f): the daily full pass is the host's. A plain member household
    asks its watermarks for no anti-entropy, so its sessions stay
    incremental past the day; without an authority check, as before."""
    marks = _Marks()
    svc, _posts = _inc_provider(encoder, marks)
    if authority is not None:

        async def _check(_space_id: str) -> bool:
            return authority

        svc.attach_roster_authority(_check)
    await svc.stream_initial(_FakeSession(sync_mode="incremental", have_seq=33))
    assert marks.planned[0]["anti_entropy"] is anti_entropy


async def test_an_incremental_session_streams_only_changed_rows_of_covered_resources(
    encoder,
):
    marks = _Marks(snapshot=40, since=7)
    svc, posts = _inc_provider(encoder, marks)
    session = _FakeSession(sync_mode="incremental", have_seq=33)
    await svc.stream_initial(session)
    streamed = _records(session)
    # Covered: only what changed. Kept full (members): everything.
    assert streamed["posts"] == [{"id": "p-2"}]
    assert streamed["members"] == [{"user_id": "u-1", "role": "member"}]
    assert posts.asked_since == [7]
    plan = marks.planned[0]
    assert plan["space_id"] == "sp-1" and plan["instance_id"] == "peer-r"
    assert plan["sync_mode"] == "incremental"
    # The requester's echo reaches the watermark decision.
    assert plan["have_seq"] == 33
    assert plan["shape"].endswith("|members,posts")
    assert (session.snapshot_seq, session.since_seq) == (40, 7)
    assert session.stream_clean is True
    # The signed sentinel names how many chunks came before it, so the
    # requester can tell a chunk lost on the way.
    sentinel = orjson.loads(session.rtc.sent[-1])
    assert sentinel["resource"] == SENTINEL_RESOURCE
    assert sentinel["chunk_count"] == len(session.rtc.sent) - 1
    # … and the snapshot it streamed up to: the requester's echo
    # (``have_seq``) once it applied the stream cleanly.
    assert sentinel["snapshot_seq"] == 40


async def test_a_member_household_streams_only_its_own_changed_pictures(encoder):
    """Migration 0122 + #2(a): an incremental session from a plain member
    household streams only the pictures changed since the watermark, and
    of those only its own people's."""
    marks = _Marks(snapshot=40, since=7)
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    pictures = _IncExporter(
        "member_pictures",
        [{"user_id": "u-ours"}, {"user_id": "u-hosts"}, {"user_id": "u-ours-2"}],
        changed=[{"user_id": "u-ours"}, {"user_id": "u-hosts"}],
    )
    svc = SpaceSyncService(
        builder=builder,
        exporters={"member_pictures": pictures},
        sig_suite="ed25519",
        watermarks=marks,
    )

    async def _no(_space_id: str) -> bool:
        return False

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours", "u-ours-2"}

    svc.attach_roster_authority(_no, own_members=_ours)
    session = _FakeSession(sync_mode="incremental", have_seq=33)
    await svc.stream_initial(session)
    assert _records(session)["member_pictures"] == [{"user_id": "u-ours"}]
    assert pictures.asked_since == [7]


async def test_no_watermark_streams_everything(encoder):
    marks = _Marks(snapshot=40, since=None)
    svc, posts = _inc_provider(encoder, marks)
    session = _FakeSession(sync_mode="incremental")
    await svc.stream_initial(session)
    assert [r["id"] for r in _records(session)["posts"]] == ["p-1", "p-2", "p-3"]
    assert posts.asked_since == []


async def test_confirm_advances_to_the_snapshot_only_after_a_clean_stream(encoder):
    marks = _Marks(snapshot=40, since=7)
    svc, _ = _inc_provider(encoder, marks)
    session = _FakeSession(sync_mode="incremental")
    # Not streamed yet: nothing to confirm.
    await svc.confirm_complete(session)
    assert marks.confirmed == []
    await svc.stream_initial(session)
    await svc.confirm_complete(session)
    assert marks.confirmed == [
        {
            "space_id": "sp-1",
            "instance_id": "peer-r",
            "seq": 40,
            "shape": session.shape,
            "full": False,
        }
    ]


async def test_a_full_stream_confirms_as_full(encoder):
    marks = _Marks(snapshot=12, since=None)
    svc, _ = _inc_provider(encoder, marks)
    session = _FakeSession(sync_mode="initial")
    await svc.stream_initial(session)
    await svc.confirm_complete(session)
    assert marks.confirmed[0]["full"] is True and marks.confirmed[0]["seq"] == 12


async def test_a_stream_with_a_failed_chunk_never_advances_the_watermark(encoder):

    marks = _Marks(snapshot=40, since=None)
    svc, _ = _inc_provider(encoder, marks)
    federation = AsyncMock()
    federation.space_member_version = AsyncMock(return_value=55)
    ok = SimpleNamespace(ok=True, error=None, retry_after_s=None)
    bad = SimpleNamespace(ok=False, error="routed_send_failed", retry_after_s=None)
    # One failed chunk (under the abandon budget), then success: the stream
    # completes and the sentinel ships, but a row may be missing.
    federation.send_with_mesh_fallback = AsyncMock(side_effect=[bad, ok, ok, ok, ok])
    svc.attach_federation(federation)
    session = _FakeSession(sync_mode="incremental")
    session.rtc = None
    session.transport_mode = "https"
    await svc.stream_initial(session)
    assert session.stream_clean is False
    await svc.confirm_complete(session)
    assert marks.confirmed == []


async def test_the_snapshot_is_read_before_any_row(encoder):
    order: list[str] = []

    class _OrderedMarks(_Marks):
        async def snapshot(self) -> int:
            order.append("snapshot")
            return 40

    class _OrderedExporter(_FakeExporter):
        async def list_records(self, space_id):
            order.append(f"read:{self.resource}")
            return await super().list_records(space_id)

    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={"members": _OrderedExporter("members", [{"user_id": "u"}])},
        watermarks=_OrderedMarks(),
    )
    await svc.stream_initial(_FakeSession(sync_mode="incremental"))
    assert order == ["snapshot", "read:members"]


async def test_the_shape_follows_peer_version_retention_and_resources(encoder):

    class _Windows:
        async def retention_key(self, space_id: str) -> str:
            return "30:event"

        async def for_space(self, space_id: str):

            return KEEP_FOREVER

    marks = _Marks()
    svc, _ = _inc_provider(encoder, marks, windows=_Windows())
    federation = AsyncMock()
    federation.space_member_version = AsyncMock(return_value=55)
    federation.peer_supports = AsyncMock(return_value=True)
    svc.attach_federation(federation)
    session = _FakeSession(sync_mode="incremental")
    await svc.stream_initial(session)
    assert session.shape.split("|")[1:] == ["p55", "r30:event", "members,posts"]


async def test_without_watermarks_every_session_streams_in_full(provider):
    session = _FakeSession(sync_mode="incremental")
    await provider.stream_initial(session)
    assert getattr(session, "since_seq", None) is None
    await provider.confirm_complete(session)  # a no-op, never raises


class _Toggle(_FakeExporter):
    """A resource that streams only while a space feature is on."""

    def __init__(self, resource: str, records: list[dict]) -> None:
        super().__init__(resource, records)
        self.on = False

    async def is_active(self, space_id: str) -> bool:
        return self.on


async def test_a_resource_switched_on_changes_the_shape_so_it_streams_in_full(
    encoder,
):
    """The space chat turned off and on again: while off it is not part of
    the session shape, so turning it on makes the next periodic session a
    full stream — the chat said meanwhile reaches the household."""
    marks = _Marks()
    svc, _ = _inc_provider(encoder, marks)
    toggle = _Toggle("timetables", [{"id": "t-1"}])
    svc._exporters["timetables"] = toggle
    off = _FakeSession(sync_mode="incremental")
    await svc.stream_initial(off)
    assert "timetables" not in _records(off)
    assert not off.shape.endswith("timetables")
    toggle.on = True
    on = _FakeSession(sync_mode="incremental")
    await svc.stream_initial(on)
    assert on.shape.endswith("|members,posts,timetables")
    assert on.shape != off.shape
    assert _records(on)["timetables"] == [{"id": "t-1"}]


async def test_the_requesters_version_is_read_once_per_session(encoder):
    """Every version gate of a session — the calendar's record set, the chat,
    the shape, the chunk numbering — follows ONE reading of the requester's
    version, so they cannot disagree mid-session."""
    from unittest.mock import AsyncMock

    svc = _chat_provider(encoder)
    federation = AsyncMock()
    federation.space_member_version = AsyncMock(return_value=56)
    svc.attach_federation(federation)

    async def _gate(space_id: str, instance_id: str) -> bool:
        return True

    svc.attach_member_gate(_gate)
    session = _FakeSession()
    await svc.stream_initial(session)
    assert federation.space_member_version.await_count == 1
    chunk = orjson.loads(session.rtc.sent[0])
    assert (
        orjson.loads(base64.urlsafe_b64decode(chunk["encrypted_payload"]))[
            "chunk_index"
        ]
        == 0
    )
    # A REQUEST_MORE slice is numbered on from the stream it follows.
    await svc.stream_request_more(session, {"resource": "chat_messages"})
    more = orjson.loads(session.rtc.sent[-1])
    assert (
        orjson.loads(base64.urlsafe_b64decode(more["encrypted_payload"]))["chunk_index"]
        == 1
    )
    assert federation.space_member_version.await_count == 1


async def test_a_request_more_slice_mid_stream_takes_fresh_indices(encoder):
    """A REQUEST_MORE slice streamed while the stream runs draws from the
    session's one index counter: no index is reused, and the sentinel counts
    every index the session handed out."""
    from unittest.mock import AsyncMock

    svc = _chat_provider(encoder)
    federation = AsyncMock()
    federation.space_member_version = AsyncMock(return_value=56)
    svc.attach_federation(federation)

    async def _gate(space_id: str, instance_id: str) -> bool:
        return True

    svc.attach_member_gate(_gate)
    session = _FakeSession()

    class _Rtc(_FakeRtc):
        fired = False

        async def send_chunk(self, data):
            await super().send_chunk(data)
            if not self.fired:
                self.fired = True
                await svc.stream_request_more(session, {"resource": "chat_messages"})

    session.rtc = _Rtc()
    await svc.stream_initial(session)
    frames = [orjson.loads(raw) for raw in session.rtc.sent]
    content = [f for f in frames if f["resource"] != SENTINEL_RESOURCE]
    indices = [
        orjson.loads(base64.urlsafe_b64decode(f["encrypted_payload"]))["chunk_index"]
        for f in content
    ]
    assert sorted(indices) == list(range(len(content))) and len(content) == 2
    (sentinel,) = [f for f in frames if f["resource"] == SENTINEL_RESOURCE]
    assert sentinel["chunk_count"] == len(content)


# ─── #2(c): the roster resources stream only when they changed ────────────


def _roster_inc_provider(encoder, marks):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    members = _FakeExporter("members", [{"user_id": "u-1", "role": "member"}])
    bans = _FakeExporter("bans", [{"user_id": "u-x"}])
    posts = _IncExporter("posts", [{"id": "p-1"}], [{"id": "p-1"}])
    svc = SpaceSyncService(
        builder=builder,
        exporters={"bans": bans, "members": members, "posts": posts},
        sig_suite="ed25519",
        watermarks=marks,
    )
    return svc, members


async def _session(svc, marks, *, since, snapshot, confirm=True, requester="peer-r"):
    marks._since = since
    marks._snapshot = snapshot
    session = _FakeSession(
        sync_mode="incremental" if since is not None else "initial",
        requester=requester,
    )
    await svc.stream_initial(session)
    if confirm:
        await svc.confirm_complete(session)
    return session


async def test_the_roster_streams_once_then_only_when_it_changes(encoder):
    """A periodic session re-streamed every ``members`` / ``bans`` row each
    time (~50 rows to each of 14 households, 48 times a day). Now the
    provider remembers what it last streamed to each household, and an
    incremental session whose watermark still covers that stream sends
    them only when they changed. The session shape keeps both resources,
    so the watermark holds."""
    marks = _Marks()
    svc, members = _roster_inc_provider(encoder, marks)
    first = await _session(svc, marks, since=None, snapshot=40)
    assert set(_records(first)) == {"bans", "members", "posts"}
    quiet = await _session(svc, marks, since=40, snapshot=50)
    assert set(_records(quiet)) == {"posts"}
    assert marks.planned[1]["shape"] == marks.planned[0]["shape"]
    # Still quiet on the next one (the skip confirmed the same records).
    again = await _session(svc, marks, since=50, snapshot=60)
    assert set(_records(again)) == {"posts"}
    members._records = [{"user_id": "u-1", "role": "admin"}]
    moved = await _session(svc, marks, since=60, snapshot=70)
    assert _records(moved)["members"] == [{"user_id": "u-1", "role": "admin"}]
    assert "bans" not in _records(moved)


async def test_the_same_roster_in_another_order_is_no_change(encoder):
    """Rows tied on ``joined_at`` may list in any order — no re-stream."""
    marks = _Marks()
    svc, members = _roster_inc_provider(encoder, marks)
    members._records = [{"user_id": "u-1"}, {"user_id": "u-2"}]
    await _session(svc, marks, since=None, snapshot=40)
    members._records = [{"user_id": "u-2"}, {"user_id": "u-1"}]
    quiet = await _session(svc, marks, since=40, snapshot=50)
    assert "members" not in _records(quiet)


async def test_a_rolled_back_household_gets_the_roster_again(encoder):
    """A household restored from a backup echoes an older ``have_seq``, so
    its session streams from before the stream it confirmed: the roster
    goes again even though it did not change here."""
    marks = _Marks()
    svc, _members = _roster_inc_provider(encoder, marks)
    await _session(svc, marks, since=None, snapshot=40)
    rolled = await _session(svc, marks, since=30, snapshot=50)
    assert {"bans", "members"} <= set(_records(rolled))


async def test_an_unconfirmed_stream_does_not_spare_the_next_one(encoder):
    """Only a stream the household confirmed clean counts as held — and a
    restarted provider (nothing remembered) streams the roster once."""
    marks = _Marks()
    svc, _members = _roster_inc_provider(encoder, marks)
    await _session(svc, marks, since=None, snapshot=40, confirm=False)
    nxt = await _session(svc, marks, since=40, snapshot=50)
    assert {"bans", "members"} <= set(_records(nxt))


async def test_the_roster_memory_is_per_household(encoder):
    marks = _Marks()
    svc, _members = _roster_inc_provider(encoder, marks)
    await _session(svc, marks, since=None, snapshot=40)
    other = await _session(svc, marks, since=40, snapshot=50, requester="peer-o")
    assert {"bans", "members"} <= set(_records(other))


# ─── #2(d): is there anything to stream? ──────────────────────────────────


async def test_a_session_with_nothing_changed_is_quiet(encoder):
    """The BEGIN handler asks before it negotiates WebRTC: a periodic
    session whose covered resources changed nothing and whose roster the
    requester already holds (#2(c)) has only its sentinel to send."""
    marks = _Marks()
    svc, _members = _roster_inc_provider(encoder, marks)
    await _session(svc, marks, since=None, snapshot=40)
    svc._exporters["posts"]._changed = []
    marks._since = 40
    assert await svc.is_quiet(_FakeSession(sync_mode="incremental")) is True
    # Something changed: not quiet.
    svc._exporters["posts"]._changed = [{"id": "p-9"}]
    assert await svc.is_quiet(_FakeSession(sync_mode="incremental")) is False


async def test_a_full_session_or_a_moved_roster_is_never_quiet(encoder):
    marks = _Marks()
    svc, members = _roster_inc_provider(encoder, marks)
    svc._exporters["posts"]._changed = []
    marks._since = None
    assert await svc.is_quiet(_FakeSession()) is False
    await _session(svc, marks, since=None, snapshot=40)
    members._records = [{"user_id": "u-2"}]
    marks._since = 40
    assert await svc.is_quiet(_FakeSession(sync_mode="incremental")) is False


async def test_a_failing_quiet_check_is_not_quiet(encoder):
    marks = _Marks()
    svc, _members = _roster_inc_provider(encoder, marks)

    async def _boom(**_kw):
        raise RuntimeError("db gone")

    marks.since_for = _boom
    assert await svc.is_quiet(_FakeSession(sync_mode="incremental")) is False


async def test_without_watermarks_nothing_is_quiet(encoder):
    svc = _roster_provider(encoder)
    assert await svc.is_quiet(_FakeSession(sync_mode="incremental")) is False


# ─── Step 16: a member household streams only its own people's content ──


def _content_provider(encoder) -> SpaceSyncService:
    ours, theirs = "u-ours", "u-hosts"
    exporters = {
        "posts": _FakeExporter(
            "posts", [{"id": "p-1", "author": ours}, {"id": "p-2", "author": theirs}]
        ),
        "comments": _FakeExporter(
            "comments",
            [{"id": "c-1", "author": theirs}, {"id": "c-2", "author": ours}],
        ),
        "pages": _FakeExporter(
            "pages",
            [
                {"id": "pg-1", "created_by": theirs},
                {"id": "pg-2", "created_by": ""},  # creator unknown: kept
            ],
        ),
        "stickies": _FakeExporter(
            "stickies",
            [{"id": "s-1", "created_by": ours}, {"id": "s-2", "author": theirs}],
        ),
        "gallery": _FakeExporter(
            "gallery",
            [
                {"id": "a-1", "kind": "album", "owner_user_id": ours},
                {"id": "a-2", "kind": "album", "owner_id": theirs},
                {"id": "i-1", "uploaded_by": theirs},
                {"id": "i-2", "uploader": ours},
            ],
        ),
        "bazaar": _FakeExporter(
            "bazaar",
            [
                {"post_id": "p-1", "seller_user_id": ours},
                {"post_id": "p-2", "seller_user_id": theirs},
            ],
        ),
        # A removal: a moderator household may remove anyone's row, and the
        # receiver judges that itself — never narrowed.
        "posts_deleted": _FakeExporter("posts_deleted", [{"id": "p-9"}]),
    }
    return SpaceSyncService(
        builder=ChunkBuilder(encoder=encoder, crypto=_FakeCrypto()),
        exporters=exporters,
        sig_suite="ed25519",
    )


@pytest.mark.parametrize("authority", [False, True])
async def test_a_member_household_streams_only_its_own_peoples_content(
    encoder, authority
):
    """Every receiver takes from a household that isn't the host only the
    rows its own people wrote (``receiver._admit``: ``acts_for``). A plain
    member household mirrors the whole space, so it streamed every
    household's rows to every co-member, all refused. The host still
    streams everything."""
    svc = _content_provider(encoder)

    async def _authority(_space_id: str) -> bool:
        return authority

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours"}

    svc.attach_roster_authority(_authority, own_members=_ours)
    session = _FakeSession()
    await svc.stream_initial(session)
    ids = {
        resource: [r.get("id") or r.get("post_id") for r in rows]
        for resource, rows in _records(session).items()
    }
    if authority:
        assert ids["posts"] == ["p-1", "p-2"]
        assert ids["gallery"] == ["a-1", "a-2", "i-1", "i-2"]
        return
    assert ids == {
        "posts": ["p-1"],
        "comments": ["c-2"],
        "pages": ["pg-2"],
        "stickies": ["s-1"],
        "gallery": ["a-1", "i-2"],
        "bazaar": ["p-1"],
        "posts_deleted": ["p-9"],
    }


# ── v_80 — link-joined members: descriptors in the records, no byte push ─


class _ReaderPlan:
    def __init__(self, urls):
        self.urls = list(urls)

    def wire(self):
        return {
            u.rsplit("/", 1)[-1]: {
                "blob_id": "a" * 64,
                "suite": "s",
                "epoch": 0,
                "size": 9,
            }
            for u in self.urls
        }


class _ReaderUploader:
    def __init__(self, readers=("peer-e",)):
        self.readers = set(readers)
        self.planned: list = []
        self.items: list = []
        self.uploaded: list = []

    async def is_relay_reader(self, iid):
        return iid in self.readers

    async def plan_for_reader(self, space_id, urls, iid, *, item_id):
        urls = list(urls)
        self.planned.append((space_id, urls, iid))
        self.items.append(item_id)
        return _ReaderPlan(urls) if urls else None

    def upload(self, plan):
        self.uploaded.append(plan)


def _streamed_records(session, resource):
    out = []
    for raw in session.rtc.sent:
        env = orjson.loads(raw)
        if env["resource"] != resource:
            continue
        out.extend(
            orjson.loads(base64.urlsafe_b64decode(env["encrypted_payload"]))["records"]
        )
    return out


def _media_provider(encoder, uploader):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "posts": _FakeExporter(
            "posts",
            [
                {"id": "p-1", "author": "u-1", "image_urls": ["api/media/a.webp"]},
                {"id": "p-2", "author": "u-1"},
            ],
        ),
        "calendar": _FakeExporter(
            "calendar", [{"id": "e-1", "cover_url": "api/media/c.webp"}]
        ),
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
    }
    svc = SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")
    svc.attach_media_gfs(uploader)
    return svc


async def test_a_link_joined_v80_requesters_records_carry_media_refs(encoder):
    uploader = _ReaderUploader()
    svc = _media_provider(encoder, uploader)
    session = _FakeSession(requester="peer-e")
    await svc.stream_initial(session)
    posts = {r["id"]: r for r in _streamed_records(session, "posts")}
    assert posts["p-1"]["media_refs"] == {
        "a.webp": {"blob_id": "a" * 64, "suite": "s", "epoch": 0, "size": 9}
    }
    assert "media_refs" not in posts["p-2"]
    (event,) = _streamed_records(session, "calendar")
    assert set(event["media_refs"]) == {"c.webp"}
    # Each blob is sealed for the record's own item (in its AAD).
    assert sorted(uploader.items) == ["e-1", "p-1"]
    assert all("media_refs" not in r for r in _streamed_records(session, "members"))
    # The blobs go to the requester's GFS (once per file).
    assert len(uploader.uploaded) == 2


async def test_another_requesters_records_carry_no_media_refs(encoder):
    uploader = _ReaderUploader()
    svc = _media_provider(encoder, uploader)
    session = _FakeSession(requester="peer-paired")
    await svc.stream_initial(session)
    assert all("media_refs" not in r for r in _streamed_records(session, "posts"))
    assert uploader.planned == []


async def test_catch_up_files_per_stream_are_bounded(encoder, monkeypatch):
    """At most ``MAX_CATCHUP_MEDIA_RECORDS`` records of each resource are
    described per stream (newest first) — the rest go without."""
    monkeypatch.setattr(provider_mod, "MAX_CATCHUP_MEDIA_RECORDS", 1)
    uploader = _ReaderUploader()
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    pictures = [
        {"id": f"p-{i}", "author": "u-1", "image_urls": [f"api/media/{i}.webp"]}
        for i in range(3)
    ]
    svc = SpaceSyncService(
        builder=builder,
        exporters={"posts": _FakeExporter("posts", pictures)},
        sig_suite="ed25519",
    )
    svc.attach_media_gfs(uploader)
    session = _FakeSession(requester="peer-e")
    await svc.stream_initial(session)
    described = [r for r in _streamed_records(session, "posts") if "media_refs" in r]
    assert [r["id"] for r in described] == ["p-0"]
    assert len(_streamed_records(session, "posts")) == 3


# ── v_82 — a space page's files in the catch-up ─────────────────────────


@dataclass
class _Page:
    id: str
    content: str = ""
    cover_image_url: str | None = None


class _PageRepo:
    def __init__(self, pages):
        self.pages = pages
        self.history: dict[str, list] = {}
        self.sides: dict[str, list] = {}

    async def list(self, *, space_id=None, since_seq=None):
        return list(self.pages) if space_id == "sp-1" else []

    async def list_versions(self, page_id, *, space_id):
        return list(self.history.get(page_id, ()))

    async def list_conflict_sides(self, page_id, *, space_id):
        return list(self.sides.get(page_id, ()))


class _VersionFed:
    def __init__(self, versions):
        self.versions = versions

    async def is_space_session_peer(self, iid):
        return False

    async def space_member_supports(self, iid, *, min_version):
        return self.versions.get(iid, 0) >= min_version

    async def space_member_version(self, iid):
        return self.versions.get(iid, 0)

    async def peer_supports(self, iid, *, min_version):
        return self.versions.get(iid, 0) >= min_version


class _PageRefs:
    elsewhere: frozenset[str] = frozenset()

    async def confined_to_space(self, name, space_id):
        return name not in self.elsewhere


_PAGES = [
    _Page("pg-1", "![a](api/media/a.webp) ![b](/api/media/b.webp)", "api/media/c.webp"),
    _Page("pg-2", "no pictures"),
]


def _page_provider(encoder, *, hosting=True, versions=None, exporters=None):
    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    media_sync.held_files = AsyncMock(side_effect=list)
    svc = SpaceSyncService(
        builder=ChunkBuilder(encoder=encoder, crypto=_FakeCrypto()),
        exporters=exporters or {},
        sig_suite="ed25519",
        media_sync=media_sync,
    )
    svc._federation = _VersionFed(versions or {"peer-new": 82, "peer-old": 80})

    async def _hosts(space_id):
        return hosting

    svc.attach_pages(_PageRepo(_PAGES), hosts=_hosts, media_refs=_PageRefs())
    return svc, media_sync


async def test_the_hosts_catchup_ships_every_pages_files(encoder):
    """A newcomer catching up from the host gets each page's cover and body
    pictures — the page records carry only their references."""
    svc, media_sync = _page_provider(encoder)
    await svc.stream_initial(_FakeSession(requester="peer-new"))
    calls = [c.kwargs for c in media_sync.enqueue_for_blob.call_args_list]
    assert [(c["correlation_id"], c["media_urls"]) for c in calls] == [
        ("pg-1", ["api/media/c.webp", "api/media/a.webp", "/api/media/b.webp"])
    ]
    assert calls[0]["target_instance_ids"] == ["peer-new"]


@pytest.mark.parametrize(
    ("hosting", "requester"), [(False, "peer-new"), (True, "peer-old")]
)
async def test_page_files_come_only_from_the_host_to_v81(encoder, hosting, requester):
    """Only the host fills a page's names on the receiver; a household below
    v_82 would keep them under names its page never shows."""
    svc, media_sync = _page_provider(encoder, hosting=hosting)
    await svc.stream_initial(_FakeSession(requester=requester))
    assert media_sync.enqueue_for_blob.call_args_list == []


async def test_a_link_joined_v81_requesters_page_records_carry_media_refs(encoder):
    uploader = _ReaderUploader(readers=("peer-e", "peer-e80"))
    exporters = {
        "pages": _FakeExporter(
            "pages",
            [
                {
                    "id": "pg-1",
                    "title": "T",
                    "content": "![a](api/media/a.webp)",
                    "cover_image_url": "api/media/c.webp",
                }
            ],
        )
    }
    svc, _media = _page_provider(
        encoder, versions={"peer-e": 82, "peer-e80": 80}, exporters=exporters
    )
    svc.attach_media_gfs(uploader)
    session = _FakeSession(requester="peer-e")
    await svc.stream_initial(session)
    (page,) = _streamed_records(session, "pages")
    assert set(page["media_refs"]) == {"c.webp", "a.webp"}
    assert uploader.items == ["pg-1"]
    old = _FakeSession(requester="peer-e80")
    await svc.stream_initial(old)
    assert all("media_refs" not in r for r in _streamed_records(old, "pages"))


async def test_a_page_file_named_outside_the_space_is_not_in_the_catchup(
    encoder, monkeypatch
):
    monkeypatch.setattr(_PageRefs, "elsewhere", frozenset({"c.webp"}))
    svc, media_sync = _page_provider(encoder)
    await svc.stream_initial(_FakeSession(requester="peer-new"))
    calls = [c.kwargs for c in media_sync.enqueue_for_blob.call_args_list]
    assert [c["media_urls"] for c in calls] == [
        ["api/media/a.webp", "/api/media/b.webp"]
    ]


async def test_the_catchup_ships_a_pages_history_and_side_files(encoder):
    """Review finding 7: a newcomer gets a page's history versions' files
    (a revert brings one back) and its open sides' files too."""
    svc, media_sync = _page_provider(encoder)
    repo = svc._page_repo
    repo.history["pg-2"] = [_Page("v1", "![o](api/media/old.webp)")]
    repo.sides["pg-2"] = [_Page("s1", "![s](api/media/side.webp)")]
    await svc.stream_initial(_FakeSession(requester="peer-new"))
    calls = {
        c.kwargs["correlation_id"]: c.kwargs["media_urls"]
        for c in media_sync.enqueue_for_blob.call_args_list
    }
    assert calls["pg-2"] == ["api/media/side.webp", "api/media/old.webp"]


async def test_a_pages_history_in_the_catchup_is_bounded(encoder, monkeypatch):
    """Review finding 4: a page's history ships its newest files only — at
    most PAGE_HISTORY_FILES per page and STREAM_HISTORY_FILES per stream; an
    older one comes back with the version that shows it again."""
    monkeypatch.setattr(provider_mod, "PAGE_HISTORY_FILES", 3)
    monkeypatch.setattr(provider_mod, "STREAM_HISTORY_FILES", 4)
    svc, media_sync = _page_provider(encoder)
    repo = svc._page_repo
    repo.pages = [_Page("pg-1"), _Page("pg-2")]
    for pid in ("pg-1", "pg-2"):
        # list_versions is oldest first; the newest history wins.
        repo.history[pid] = [
            _Page(f"v{i}", f"![h](api/media/{pid}-h{i}.webp)") for i in range(10)
        ]
    await svc.stream_initial(_FakeSession(requester="peer-new"))
    calls = {
        c.kwargs["correlation_id"]: c.kwargs["media_urls"]
        for c in media_sync.enqueue_for_blob.call_args_list
    }
    assert calls["pg-1"] == [f"api/media/pg-1-h{i}.webp" for i in (9, 8, 7)]
    assert calls["pg-2"] == ["api/media/pg-2-h9.webp"]  # the stream budget


# ── v_85: the host's catch-up vouches for its files' hashes ─────────────


class _Hasher:
    def __init__(self) -> None:
        self.asked: list[list] = []

    async def hashes_for(self, urls):
        self.asked.append(list(urls))
        return {
            u.rsplit("/", 1)[-1]: FileHash(hashlib.sha256(u.encode()).hexdigest(), 7)
            for u in urls
        }


def _hash_provider(encoder, *, authority: bool):
    svc = _content_provider(encoder)
    # Pre-v_85 content: the rows carry no hashes of their own.
    svc._exporters["posts"] = _FakeExporter(
        "posts",
        [
            {"id": "p-1", "author": "u-ours", "image_urls": ["api/media/a.webp"]},
            {"id": "p-2", "author": "u-hosts"},
        ],
    )
    svc._exporters["comments"] = _FakeExporter(
        "comments", [{"id": "c-1", "author": "u-ours", "media_url": "api/media/c.webp"}]
    )

    async def _authority(_space_id: str) -> bool:
        return authority

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours"}

    svc.attach_roster_authority(_authority, own_members=_ours)
    hasher = _Hasher()
    svc.attach_file_hasher(hasher)
    return svc, hasher


async def test_the_host_attaches_hashes_to_pre_hash_content(encoder):
    svc, _hasher = _hash_provider(encoder, authority=True)
    session = _FakeSession()
    session.peer_version = 85
    await svc.stream_initial(session)
    posts = {r["id"]: r for r in _streamed_records(session, "posts")}
    want = hashlib.sha256(b"api/media/a.webp").hexdigest()
    assert posts["p-1"]["files"] == {"a.webp": {"h": want}}
    assert posts["p-1"]["files_suite"] == "sha256"
    assert "files" not in posts["p-2"]  # no files, nothing to vouch for
    (comment,) = _streamed_records(session, "comments")
    assert set(comment["files"]) == {"c.webp"}


async def test_a_non_host_provider_attaches_no_hashes(encoder):
    svc, hasher = _hash_provider(encoder, authority=False)
    session = _FakeSession()
    session.peer_version = 85
    await svc.stream_initial(session)
    for resource in ("posts", "comments"):
        assert all("files" not in r for r in _streamed_records(session, resource))
    assert hasher.asked == []


async def test_an_older_requester_gets_no_hashes(encoder):
    svc, hasher = _hash_provider(encoder, authority=True)
    session = _FakeSession()
    session.peer_version = 84
    await svc.stream_initial(session)
    assert all("files" not in r for r in _streamed_records(session, "posts"))
    assert hasher.asked == []


@pytest.mark.parametrize(("crossed", "walks"), [(True, False), (False, True)])
async def test_a_sync_line_crossing_ships_no_catchup_media(encoder, crossed, walks):
    """#1322 review: a household that already holds the space and only
    crossed a sync line (its watermark's shape is older) gets the one full
    stream of rows, not every file of the space again; a genuine newcomer
    (or a reset) still gets the media."""
    marks = _Marks(since=None)
    marks.crossed = crossed
    svc, _media_sync = _catchup_provider(
        encoder,
        [],
        exporters={"members": _FakeExporter("members", [{"user_id": "u-1"}])},
        watermarks=marks,
    )
    session = _FakeSession(sync_mode="initial", have_seq=12)
    await svc.stream_initial(session)
    assert (len(svc._item_files.asked) == 1) is walks


# ─── v_86: a trusted non-host provider fills hashed files, not rows ───────


async def test_a_trusted_non_host_full_catch_up_ships_only_its_own_peoples_files(
    encoder,
):
    """A moderator household (not the host) streams only its own people's
    rows, and its full catch-up ships only its own people's files: a full
    stream is the host's job (it ships every file), so k helpers never
    re-send the same bytes to a newcomer. Helper fills ride the bounded
    resume (``test_resume``)."""
    sc = f"api/media/{space_content_media_name('sp-1', 'a' * 64, 'x.webp')}"
    legacy = "api/media/sd-0123.webp"

    items = _ItemFiles(
        [
            ("2026-11-02 10:00:00", "p-ours", "u-ours", [sc, legacy]),
            ("2026-11-01 10:00:00", "p-theirs", "u-theirs", [sc, legacy]),
        ]
    )

    class _Spaces:
        async def list_members(self, space_id):
            return [
                SpaceMember(
                    space_id=space_id, user_id="u-ours", role="moderator", joined_at=""
                ),
                SpaceMember(
                    space_id=space_id, user_id="u-theirs", role="member", joined_at=""
                ),
            ]

        async def list_local_member_user_ids(self, space_id):
            return ["u-ours"]

    async def _no(_space_id: str) -> bool:
        return False

    async def _ours(_space_id: str) -> set[str]:
        return {"u-ours"}

    media_sync = AsyncMock()
    media_sync.held_files = AsyncMock(side_effect=list)
    svc = SpaceSyncService(
        builder=ChunkBuilder(encoder=encoder, crypto=_FakeCrypto()),
        exporters={
            "posts": _FakeExporter(
                "posts",
                [
                    {"id": "p-ours", "author": "u-ours"},
                    {"id": "p-theirs", "author": "u-theirs"},
                ],
            )
        },
        sig_suite="ed25519",
        media_sync=media_sync,
        item_files=items,
    )
    svc.attach_roster_authority(_no, own_members=_ours)
    svc.attach_file_gates(CatchUpFileGates(space_repo=_Spaces(), roster_authority=_no))
    session = _FakeSession()
    await svc.stream_initial(session)
    assert [r["id"] for r in _records(session)["posts"]] == ["p-ours"]
    shipped = {
        c.kwargs["correlation_id"]: list(c.kwargs["media_urls"])
        for c in media_sync.enqueue_for_blob.call_args_list
    }
    assert shipped == {"p-ours": [sc, legacy]}
