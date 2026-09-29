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

import base64
from datetime import datetime, timezone

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)

pytestmark = pytest.mark.asyncio

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
        timestamp=datetime.now(timezone.utc).isoformat(),
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
            "api/media/a1.webp",
            '["api/media/a2.webp"]',
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
        " 'u-a', 'offer', 'Bike', '[\"api/media/bz1.webp\"]',"
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
            (iid, aid, f"{fn}.webp", f"{fn}-t.webp"),
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
    assert (media / filename).read_bytes() == b"NEW-BYTES"


async def test_legacy_post_id_only_payload_is_written(env):
    svc, media = env
    await svc._on_space_media_blob(_blob({"post_id": "post-a", "filename": "a1.webp"}))
    assert (media / "a1.webp").read_bytes() == b"NEW-BYTES"


async def test_payload_space_id_fallback_when_routing_absent(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-a", "space_id": "sp-a", "filename": "a1.webp"},
            space_id=None,
        )
    )
    assert (media / "a1.webp").read_bytes() == b"NEW-BYTES"


# ── Not the envelope's space / not referenced → refused ─────────────────


@pytest.mark.parametrize(
    ("correlation", "filename"),
    [
        ("post-b", "b1.webp"),  # post of another space
        ("gi-b", "gb.webp"),  # gallery item of another space
        ("post-a", "b1.webp"),  # gated post, but not its file
        ("post-bz", "a1.webp"),  # listing known, file not on it
        ("gi-a", "a1.webp"),  # gallery item known, file not on it
    ],
)
async def test_media_outside_gated_scope_is_refused(env, caplog, correlation, filename):
    svc, media = env
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob(
                {
                    "post_id": correlation,
                    "correlation_id": correlation,
                    "filename": filename,
                }
            )
        )
    assert not (media / filename).exists()
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
    (media / "a1.webp").write_bytes(b"ORIGINAL")
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob({"post_id": "post-a", "filename": "a1.webp"})
        )
    assert (media / "a1.webp").read_bytes() == b"ORIGINAL"
    assert "not replacing" in caplog.text


async def test_identical_redelivery_is_a_quiet_noop(env, caplog):
    svc, media = env
    (media / "a1.webp").write_bytes(b"NEW-BYTES")
    with caplog.at_level("WARNING"):
        await svc._on_space_media_blob(
            _blob({"post_id": "post-a", "filename": "a1.webp"})
        )
    assert (media / "a1.webp").read_bytes() == b"NEW-BYTES"
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
    (media / "a1.webp").write_bytes(b"ORIGINAL")
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
                },
                body=b"PART%d" % idx,
            )
        )
    assert (media / "a1.webp").read_bytes() == b"ORIGINAL"
    partial = media / ".partial"
    assert not partial.exists() or list(partial.iterdir()) == []


# ── Blob before its row: write-once, so the legit order still works ────


async def test_blob_before_post_is_written_once(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-later", "filename": "later.webp"})
    )
    assert (media / "later.webp").read_bytes() == b"NEW-BYTES"
    await svc._on_space_media_blob(
        _blob({"post_id": "post-later", "filename": "later.webp"}, body=b"SECOND")
    )
    assert (media / "later.webp").read_bytes() == b"NEW-BYTES"


async def test_blob_for_bazaar_post_before_listing_is_written(env):
    svc, media = env
    await svc._on_space_media_blob(
        _blob({"post_id": "post-bz-pending", "filename": "bzp.webp"})
    )
    assert (media / "bzp.webp").read_bytes() == b"NEW-BYTES"


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
                },
                body=chunk,
            )
        )
    assert (media / "a1.webp").read_bytes() == full
    assert list((media / ".partial").iterdir()) == []


async def test_chunks_from_another_household_never_mix(env):
    """Two households sending the same transfer id assemble separately."""
    svc, media = env
    payload = {
        "post_id": "post-a",
        "filename": "a1.webp",
        "transfer_id": "a1.webp:self",
        "chunk_count": 2,
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
    assert (media / "a1.webp").read_bytes() == b"GOOD0GOOD1"


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
    assert not (media / "a1.webp").exists()


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
            '{"url": "https://example.com/", "title": "T",'
            ' "thumbnail_url": "api/media/lp1.webp"}',
        ),
    )
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-lp", "correlation_id": "post-lp", "filename": "lp1.webp"}
        )
    )
    assert (media / "lp1.webp").read_bytes() == b"NEW-BYTES"
    await svc._on_space_media_blob(
        _blob(
            {"post_id": "post-lp", "correlation_id": "post-lp", "filename": "zz.webp"}
        )
    )
    assert not (media / "zz.webp").exists()


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
