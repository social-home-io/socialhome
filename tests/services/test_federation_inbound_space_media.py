"""Receiver-side ``SPACE_MEDIA_BLOB`` scope + write-once rules.

A space-media blob is bytes addressed by filename. The rules the handler
enforces (see ``docs/protocol/media.md``):

* the envelope must name one space (``resolve_space_id``);
* when the correlated row (post, bazaar listing, gallery item) is already
  known here it must live in that space and reference the filename;
* a file that already exists under the filename is never replaced —
  whatever the row lookup said, and however the bytes are chunked;
* chunks from different households never assemble into one file.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import SpaceModerationItem
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.services import inbound_media_store
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)
from socialhome.services.inbound_media_store import (
    MAX_MEDIA_CHUNKS,
    SPACE_MEDIA_MAX_BYTES,
    partial_key,
    space_direct_media_name,
)

pytestmark = pytest.mark.asyncio


def L(name: str, space_id: str = "sp-a", sender: str = "peer-a") -> str:
    """The name a file ``sender`` delivers takes here (bytes and rows alike)."""
    return space_direct_media_name(space_id, sender, name)


FET = FederationEventType


def _blob(
    payload: dict,
    *,
    space_id: str | None = "sp-a",
    from_instance: str = "peer-a",
    body: bytes | None = b"NEW-BYTES",
) -> FederationEvent:
    p = dict(payload)
    if body is not None:
        p["bytes_b64"] = base64.b64encode(body).decode("ascii")
    return FederationEvent(
        msg_id="m",
        event_type=FET.SPACE_MEDIA_BLOB,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(UTC).isoformat(),
        payload=p,
        space_id=space_id,
    )


@pytest.fixture
async def env(db, bus, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        gallery_repo=SqliteGalleryRepo(db),
        bazaar_repo=SqliteBazaarRepo(db),
        media_dir=media,
    )
    for sid in ("sp-a", "sp-b"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, sid, "host", "anna", "00" * 32),
        )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, media_url,"
        " image_urls_json) VALUES(?,?,?,?,?,?,?)",
        (
            "post-a",
            "sp-a",
            "u-a",
            "image",
            "",
            f"api/media/{L('a1.webp')}",
            f'["api/media/{L("a2.webp")}"]',
        ),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, media_url)"
        " VALUES(?,?,?,?,?,?)",
        ("post-b", "sp-b", "u-b", "image", "", "api/media/b1.webp"),
    )
    # Bazaar wrapper post in A — the photos live on the listing row.
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('post-bz', 'sp-a', 'u-a', 'bazaar', '')",
        (),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, image_urls_json, end_time, currency) VALUES('post-bz', 'sp-a',"
        f" 'u-a', 'offer', 'Bike', '[\"api/media/{L('bz1.webp')}\"]',"
        " '2099-01-01T00:00:00', 'EUR')",
        (),
    )
    # Bazaar wrapper post whose listing has not arrived yet.
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('post-bz-pending', 'sp-a', 'u-a', 'bazaar', '')",
        (),
    )
    for aid, sid in (("album-a", "sp-a"), ("album-b", "sp-b")):
        await db.enqueue(
            "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
            " item_count) VALUES(?, ?, 'u-x', 'Album', 1)",
            (aid, sid),
        )
    for iid, aid, fn in (("gi-a", "album-a", "ga"), ("gi-b", "album-b", "gb")):
        await db.enqueue(
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height)"
            " VALUES(?, ?, 'u-x', 'photo', ?, ?, 1, 1)",
            (iid, aid, L(f"{fn}.webp"), L(f"{fn}-t.webp")),
        )
    return svc, media


# ── Referenced in the gated space → written ─────────────────────────────


@pytest.mark.parametrize(
    ("correlation", "filename"),
    [
        ("post-a", "a1.webp"),  # post media_url
        ("post-a", "a2.webp"),  # post image_urls
        ("post-bz", "bz1.webp"),  # bazaar listing photo
        ("gi-a", "ga.webp"),  # gallery item
        ("gi-a", "ga-t.webp"),  # gallery thumbnail
    ],
)
async def test_referenced_media_in_gated_space_is_written(env, correlation, filename):
    svc, media = env
    await svc._on_space_media_blob(
        _blob(
            {
                "post_id": correlation,
                "correlation_id": correlation,
                "filename": filename,
            }
        )
    )
    assert (media / L(filename)).read_bytes() == b"NEW-BYTES"


async def test_legacy_post_id_only_payload_is_written(env):
    svc, media = env
    await svc._on_space_media_blob(_blob({"post_id": "post-a", "filename": "a1.webp"}))
    assert (media / L("a1.webp")).read_bytes() == b"NEW-BYTES"


async def test_payload_space_id_fallback_when_routing_absent(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-a", "space_id": "sp-a", "filename": "a1.webp"},
            space_id=None,
        )
    )
    assert (media / L("a1.webp")).read_bytes() == b"NEW-BYTES"


# ── Not the envelope's space / not referenced → refused ─────────────────


@pytest.mark.parametrize(
    ("correlation", "filename", "level"),
    [
        ("post-b", "b1.webp", "WARNING"),  # post of another space
        ("gi-b", "gb.webp", "WARNING"),  # gallery item of another space
        # Known here, not its file: routine when another household delivered
        # the row (the host's catch-up of an author's post) — INFO.
        ("post-a", "b1.webp", "INFO"),  # gated post, but not its file
        ("post-bz", "a1.webp", "INFO"),  # listing known, file not on it
        ("gi-a", "a1.webp", "INFO"),  # gallery item known, file not on it
    ],
)
async def test_media_outside_gated_scope_is_refused(
    env, caplog, correlation, filename, level
):
    svc, media = env
    with caplog.at_level(level):
        await svc._on_space_media_blob(
            _blob(
                {
                    "post_id": correlation,
                    "correlation_id": correlation,
                    "filename": filename,
                }
            )
        )
    assert not (media / L(filename)).exists()
    assert "refusing" in caplog.text


async def test_routing_and_payload_space_mismatch_is_refused(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-b", "space_id": "sp-b", "filename": "b1.webp"})
    )
    assert not (media / "b1.webp").exists()


async def test_no_space_at_all_is_refused(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-x", "filename": "x.webp"}, space_id=None)
    )
    assert not (media / "x.webp").exists()


async def test_no_correlation_id_is_refused(env):
    svc, media = env
    await svc._on_space_media_blob(_blob({"filename": "x.webp"}))
    assert not (media / "x.webp").exists()


@pytest.mark.parametrize(
    "bad",
    ["../escape.webp", "sub/dir.webp", ".hidden.webp", "a\\b.webp", "nul\x00.webp", ""],
)
async def test_unsafe_filename_is_refused(env, bad):
    svc, media = env
    await svc._on_space_media_blob(_blob({"post_id": "post-x", "filename": bad}))
    assert list(media.iterdir()) == []


# ── Write-once: an existing file is never replaced ──────────────────────


async def test_existing_file_is_not_overwritten_single_chunk(env, caplog):
    svc, media = env
    (media / L("a1.webp")).write_bytes(b"ORIGINAL")
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob({"post_id": "post-a", "filename": "a1.webp"})
        )
    assert (media / L("a1.webp")).read_bytes() == b"ORIGINAL"
    assert "not replacing" in caplog.text


async def test_identical_redelivery_is_a_quiet_noop(env, caplog):
    svc, media = env
    (media / L("a1.webp")).write_bytes(b"NEW-BYTES")
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob({"post_id": "post-a", "filename": "a1.webp"})
        )
    assert (media / L("a1.webp")).read_bytes() == b"NEW-BYTES"
    assert "not replacing" not in caplog.text


async def test_existing_file_is_not_overwritten_for_unknown_row(env):
    """A household's own file (a DM attachment, another space's picture)
    under a name the sender claims for a row we haven't seen yet."""
    svc, media = env
    (media / "m-dm.webp").write_bytes(b"ORIGINAL")
    await svc._on_space_media_blob(
        _blob({"post_id": "post-unseen", "filename": "m-dm.webp"})
    )
    assert (media / "m-dm.webp").read_bytes() == b"ORIGINAL"


async def test_existing_file_is_not_overwritten_multi_chunk(env):
    svc, media = env
    (media / L("a1.webp")).write_bytes(b"ORIGINAL")
    for idx in range(2):
        await svc._on_space_media_blob(
            _blob(
                {
                    "post_id": "post-a",
                    "filename": "a1.webp",
                    "transfer_id": "a1.webp:self",
                    "chunk_index": idx,
                    "chunk_count": 2,
                    "final": idx == 1,
                    "file_size_bytes": 10,
                },
                body=b"PART%d" % idx,
            )
        )
    assert (media / L("a1.webp")).read_bytes() == b"ORIGINAL"
    partial = media / ".partial"
    assert not partial.exists() or list(partial.iterdir()) == []


# ── Blob before its row: write-once, so the legit order still works ────


async def test_blob_before_post_is_written_once(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-later", "filename": "later.webp"})
    )
    assert (media / L("later.webp")).read_bytes() == b"NEW-BYTES"
    await svc._on_space_media_blob(
        _blob({"post_id": "post-later", "filename": "later.webp"}, body=b"SECOND")
    )
    assert (media / L("later.webp")).read_bytes() == b"NEW-BYTES"


async def test_blob_for_bazaar_post_before_listing_is_written(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-bz-pending", "filename": "bzp.webp"})
    )
    assert (media / L("bzp.webp")).read_bytes() == b"NEW-BYTES"


# ── Chunk assembly ──────────────────────────────────────────────────────


async def test_multi_chunk_assembles_referenced_file(env):
    svc, media = env
    full = b"".join(bytes([i]) * 256 for i in range(8))
    chunks = [full[i * 512 : (i + 1) * 512] for i in range(4)]
    for idx, chunk in enumerate(chunks):
        await svc._on_space_media_blob(
            _blob(
                {
                    "post_id": "post-a",
                    "filename": "a1.webp",
                    "transfer_id": "a1.webp:self",
                    "chunk_index": idx,
                    "chunk_count": 4,
                    "final": idx == 3,
                    "file_size_bytes": len(full),
                },
                body=chunk,
            )
        )
    assert (media / L("a1.webp")).read_bytes() == full
    assert list((media / ".partial").iterdir()) == []


async def test_chunks_from_another_household_never_mix(env):
    """Two households sending the same transfer id assemble separately."""
    svc, media = env
    payload = {
        "post_id": "post-a",
        "filename": "a1.webp",
        "transfer_id": "a1.webp:self",
        "chunk_count": 2,
        "file_size_bytes": 10,
    }
    await svc._on_space_media_blob(
        _blob({**payload, "chunk_index": 0, "final": False}, body=b"GOOD0")
    )
    await svc._on_space_media_blob(
        _blob(
            {**payload, "chunk_index": 0, "final": False},
            body=b"EVIL0",
            from_instance="peer-evil",
        )
    )
    await svc._on_space_media_blob(
        _blob({**payload, "chunk_index": 1, "final": True}, body=b"GOOD1")
    )
    assert (media / L("a1.webp")).read_bytes() == b"GOOD0GOOD1"


_FOUR = [bytes([i]) * 512 for i in range(4)]


def _chunk(idx: int) -> FederationEvent:
    return _blob(
        {
            "post_id": "post-a",
            "filename": "a1.webp",
            "transfer_id": "a1.webp:self",
            "chunk_index": idx,
            "chunk_count": 4,
            "final": idx == 3,
            "file_size_bytes": 4 * 512,
        },
        body=_FOUR[idx],
    )


def _no_partials(media) -> bool:
    partial = media / ".partial"
    return not partial.exists() or list(partial.iterdir()) == []


@pytest.mark.parametrize(
    "order",
    [
        (3, 0, 1, 2),  # the final chunk first
        (0, 3, 2, 1),  # a middle chunk last
        (1, 3, 2, 0),  # chunk 0 last
        (0, 1, 2, 3),  # in order
    ],
)
async def test_multi_chunk_assembles_whichever_chunk_lands_last(env, order):
    """Regression (federation demo ``verify``, a 680 KB calendar cover from
    b never reached a): the sender dispatches a file's chunks concurrently
    (``PIPELINE_WINDOW``), so the ``final`` chunk can land before the
    others. Assembly used to run only on the final chunk — the rest then
    landed into partials nobody assembled, and the sender had already
    dropped its outbox row. Whichever chunk completes the set assembles."""
    svc, media = env
    for n, idx in enumerate(order):
        await svc._on_space_media_blob(_chunk(idx))
        if n < len(order) - 1:
            assert not (media / L("a1.webp")).exists()
    assert (media / L("a1.webp")).read_bytes() == b"".join(_FOUR)
    assert _no_partials(media)


async def test_duplicate_chunks_before_and_after_assembly_are_harmless(env):
    svc, media = env
    for idx in (3, 0, 0, 1, 3, 2, 2, 1, 3):
        await svc._on_space_media_blob(_chunk(idx))
    assert (media / L("a1.webp")).read_bytes() == b"".join(_FOUR)
    assert _no_partials(media)


async def test_chunks_completing_together_assemble_once(env):
    """Force two chunks to see the complete set at the same moment: each
    assembles into a temp file of its own, one publishes, bytes intact."""
    svc, media = env
    for idx in (3, 0):
        await svc._on_space_media_blob(_chunk(idx))
    barrier = asyncio.Barrier(2)
    real = inbound_media_store.present_parts

    async def both_complete(d):
        await asyncio.wait_for(barrier.wait(), 5)
        return await real(d)

    with patch.object(inbound_media_store, "present_parts", both_complete):
        await asyncio.gather(
            svc._on_space_media_blob(_chunk(1)), svc._on_space_media_blob(_chunk(2))
        )
    assert (media / L("a1.webp")).read_bytes() == b"".join(_FOUR)
    assert _no_partials(media)
    assert [p.name for p in media.iterdir() if p.name != ".partial"] == [L("a1.webp")]


def _attempt(idx: int, chunks: list[bytes], size: int) -> FederationEvent:
    return _blob(
        {
            "post_id": "post-a",
            "filename": "a1.webp",
            "transfer_id": "a1.webp:self",
            "chunk_index": idx,
            "chunk_count": len(chunks),
            "final": idx == len(chunks) - 1,
            "file_size_bytes": size,
        },
        body=chunks[idx],
    )


async def test_retry_chunked_differently_never_mixes_attempts(env):
    """The sender re-chunks each attempt for its tier (240 KiB over an
    older channel, 512 KiB over HTTPS) under the same ``transfer_id``.
    Attempt 1 landed parts 0-2 of 5; the retry's chunk 0 (of 2) must not
    complete the set with attempt 1's stale part 1 — that wrote a
    752-byte corrupt file, kept forever by write-once."""
    svc, media = env
    full = bytes(range(256)) * 4
    small = [full[i : i + 240] for i in range(0, len(full), 240)]
    big = [full[i : i + 512] for i in range(0, len(full), 512)]
    assert (len(small), len(big)) == (5, 2)
    for idx in (0, 1, 2):
        await svc._on_space_media_blob(_attempt(idx, small, len(full)))
    await svc._on_space_media_blob(_attempt(0, big, len(full)))
    assert not (media / L("a1.webp")).exists()
    await svc._on_space_media_blob(_attempt(1, big, len(full)))
    assert (media / L("a1.webp")).read_bytes() == full


async def test_bytes_short_of_declared_size_never_land(env):
    svc, media = env
    for idx in (1, 0):
        await svc._on_space_media_blob(_attempt(idx, [b"AAAA", b"BB"], 99))
    assert not (media / L("a1.webp")).exists()
    assert _no_partials(media)


async def test_parts_left_by_a_crash_are_dropped_by_any_chunk(env):
    svc, media = env
    (media / L("a1.webp")).write_bytes(b"".join(_FOUR))
    stale = media / ".partial" / partial_key("peer-a", "a1.webp:self", chunk_count=4)
    stale.mkdir(parents=True)
    (stale / "000002").write_bytes(b"old")
    (stale / "assembling-dead").write_bytes(b"old")
    await svc._on_space_media_blob(_chunk(0))  # not the final chunk
    assert not stale.exists()


@pytest.mark.parametrize(
    ("index", "count"),
    [(-1, 2), (2, 2), (0, -1), ("x", 2), (0, 10**9)],
)
async def test_out_of_range_chunk_metadata_is_refused(env, index, count):
    svc, media = env
    await svc._on_space_media_blob(
        _blob(
            {
                "post_id": "post-a",
                "filename": "a1.webp",
                "transfer_id": "a1.webp:self",
                "chunk_index": index,
                "chunk_count": count,
                "final": True,
            }
        )
    )
    assert not (media / L("a1.webp")).exists()


async def test_link_preview_image_of_the_post_is_written(env, db):
    """The card image the author's household re-encoded rides as a normal
    SPACE_MEDIA_BLOB and lands only because the post references it."""
    svc, media = env
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content,"
        " link_preview_json) VALUES(?,?,?,?,?,?)",
        (
            "post-lp",
            "sp-a",
            "u-a",
            "text",
            "https://example.com/",
            (
                '{"url": "https://example.com/", "title": "T",'
                f' "thumbnail_url": "api/media/{L("lp1.webp")}"}}'
            ),
        ),
    )
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-lp", "correlation_id": "post-lp", "filename": "lp1.webp"}
        )
    )
    assert (media / L("lp1.webp")).read_bytes() == b"NEW-BYTES"
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-lp", "correlation_id": "post-lp", "filename": "zz.webp"}
        )
    )
    assert not (media / L("zz.webp")).exists()


async def test_inbound_post_payload_validates_link_preview(env):
    """A received card is re-validated: a remote image URL never survives,
    a non-web URL drops the card, and nothing is fetched."""
    svc, _media = env
    base = {"id": "p1", "author": "u-a", "type": "text", "content": "x"}
    good = svc._post_from_payload(
        {
            **base,
            "link_preview": {
                "url": "https://Example.com/a#frag",
                "title": "T " * 400,
                "thumbnail_url": "https://tracker.example/pixel.png",
            },
        }
    )
    assert good is not None and good.link_preview is not None
    assert good.link_preview.url == "https://example.com/a"
    assert len(good.link_preview.title or "") <= 300
    assert good.link_preview.thumbnail_url is None
    bad = svc._post_from_payload(
        {**base, "link_preview": {"url": "javascript:alert(1)", "title": "T"}}
    )
    assert bad is not None and bad.link_preview is None
    local = svc._post_from_payload(
        {
            **base,
            "link_preview": {
                "url": "https://example.com/",
                "title": "T",
                "thumbnail_url": "api/media/ok.webp",
            },
        }
    )
    assert local is not None and local.link_preview is not None
    assert local.link_preview.thumbnail_url == "api/media/ok.webp"


@pytest.mark.parametrize("size", [None, 0, -5, "x"])
async def test_multi_chunk_without_a_declared_size_is_refused(env, caplog, size):
    """A multi-chunk blob must declare ``file_size_bytes`` (every sender
    has since #440) — else the size check could be skipped silently."""
    svc, media = env
    with caplog.at_level("WARNING"):
        for idx in (1, 0):
            payload = {
                "post_id": "post-a",
                "filename": "a1.webp",
                "transfer_id": "a1.webp:self",
                "chunk_index": idx,
                "chunk_count": 2,
                "final": idx == 1,
            }
            if size is not None:
                payload["file_size_bytes"] = size
            await svc._on_space_media_blob(_blob(payload, body=b"PART%d" % idx))
    assert not (media / L("a1.webp")).exists()
    assert _no_partials(media)
    assert "file_size_bytes" in caplog.text


# ── Whole-file size cap ─────────────────────────────────────────────────


def _first_chunk(*, count: int, size: int, body: bytes) -> FederationEvent:
    return _blob(
        {
            "post_id": "post-a",
            "filename": "a1.webp",
            "transfer_id": "a1.webp:self",
            "chunk_index": 0,
            "chunk_count": count,
            "final": False,
            "file_size_bytes": size,
        },
        body=body,
    )


async def test_chunk_of_a_file_over_the_space_cap_writes_nothing(env, caplog):
    """A space file declared over ``SPACE_MEDIA_MAX_BYTES`` is refused on
    the first chunk that lands: no part, no file."""
    svc, media = env
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _first_chunk(
                count=MAX_MEDIA_CHUNKS,
                size=SPACE_MEDIA_MAX_BYTES + 1,
                body=b"x" * 512,
            )
        )
    assert not (media / L("a1.webp")).exists()
    assert _no_partials(media)
    assert "byte cap" in caplog.text


async def test_chunk_bigger_than_its_share_writes_nothing(env, caplog):
    """A chunk can't be bigger than its share of the declared file: with
    it refused, the parts on disk stay under twice the declared size."""
    svc, media = env
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _first_chunk(count=4, size=1024, body=b"x" * 512)
        )
    assert _no_partials(media)
    assert "belong to a 1024-byte file" in caplog.text


async def test_space_file_at_the_cap_is_accepted(env):
    """Control: a chunk of a file exactly at the cap lands as a part."""
    svc, media = env
    await svc._on_space_media_blob(
        _first_chunk(count=MAX_MEDIA_CHUNKS, size=SPACE_MEDIA_MAX_BYTES, body=b"x")
    )
    assert not _no_partials(media)


@pytest.mark.parametrize(
    "name",
    [
        "dm-" + "0" * 40 + ".webp",
        "gm-" + "a" * 40 + ".webp",
        "sg-" + "b" * 40 + ".webp",
        "m-1.preview.webp",
    ],
)
async def test_reserved_name_is_refused_even_for_an_unknown_row(env, caplog, name):
    """``dm-`` (received DM attachments), ``gm-`` (GFS moment media), ``sg-``
    (a space file this household fetched from a GFS under its own name —
    #1309 re-review 1: a paired member can't plant bytes there first) and
    DM transients belong to other paths: never written as a space file."""
    svc, media = env
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob({"post_id": "post-unseen", "filename": name})
        )
    assert not (media / name).exists()
    assert "reserved name" in caplog.text


# ── A held edit's new media, correlated by its moderation item ──────────


async def _held_cover_edit(svc, *, item_id: str = "item-ev", space_id: str = "sp-a"):
    await svc._space_repo.insert_moderation_item(
        SpaceModerationItem(
            id=item_id,
            space_id=space_id,
            feature="calendar",
            action="edit",
            submitted_by="u-a",
            payload={
                "entity": "event",
                "target_id": "ev-1",
                "patch": {"cover_url": f"/api/media/{L('new-cover.webp')}"},
            },
            current_snapshot='{"cover_url": "/api/media/old-cover.webp"}',
            submitted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )


async def test_a_held_edits_new_cover_reaches_a_reviewer(env):
    """Regression: a moderated edit proposing a new calendar cover was
    correlated by the event, whose current cover is another file — every
    reviewer household refused it. Correlated by the queue item, the file
    the item proposes is accepted."""
    svc, media = env
    await _held_cover_edit(svc)
    await svc._on_space_media_blob(
        _blob({"correlation_id": "item-ev", "filename": "new-cover.webp"})
    )
    assert (media / L("new-cover.webp")).read_bytes() == b"NEW-BYTES"


@pytest.mark.parametrize(
    ("space_id", "filename"),
    [
        ("sp-a", "a1.webp"),  # a file the item doesn't propose
        ("sp-a", "old-cover.webp"),  # the snapshot is not a proposal
        ("sp-b", "new-cover.webp"),  # the item is in another space
    ],
)
async def test_a_held_items_scope_stays_tight(env, caplog, space_id, filename):
    svc, media = env
    await _held_cover_edit(svc)
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob(
                {"correlation_id": "item-ev", "filename": filename},
                space_id=space_id,
            )
        )
    assert not (media / L(filename)).exists()
