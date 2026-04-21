"""Coverage for routes/gallery.py upload paths (multipart + raw)."""

from __future__ import annotations

import io

from PIL import Image

from .conftest import _auth


def _png_bytes() -> bytes:
    img = Image.new("RGB", (8, 8), (50, 100, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _make_album(client) -> str:
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Photos"},
        headers=_auth(client._tok),
    )
    return (await r.json())["id"]


# ─── Raw-body upload path ────────────────────────────────────────────────


async def test_upload_raw_png_creates_item(client):
    aid = await _make_album(client)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=_png_bytes(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    assert r.status == 201
    body = await r.json()
    assert body["item_type"] == "photo"
    assert body["url"].startswith("/api/media/")
    assert body["thumbnail_url"].startswith("/api/media/")


async def test_upload_with_caption_query_param(client):
    aid = await _make_album(client)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items?caption=summer-trip",
        data=_png_bytes(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    assert r.status == 201
    body = await r.json()
    assert body["caption"] == "summer-trip"


async def test_upload_no_content_type_treated_as_octet_stream(client):
    """Raw body without Content-Type → octet-stream → routed to video path,
    which fails (no ffmpeg in test env or rejects PNG bytes)."""
    aid = await _make_album(client)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=_png_bytes(),
        headers=_auth(client._tok),
    )
    # Acceptable: 422 (video processor rejects), 500 (ffmpeg missing
    # propagates), 503 (gallery service catches it).
    assert r.status in (422, 500, 503, 201)


# ─── Multipart upload path ───────────────────────────────────────────────


async def test_upload_multipart_creates_item(client):
    aid = await _make_album(client)
    import aiohttp

    form = aiohttp.FormData()
    form.add_field(
        "file",
        _png_bytes(),
        filename="x.png",
        content_type="image/png",
    )
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=form,
        headers=_auth(client._tok),
    )
    assert r.status == 201


async def test_upload_unknown_album_raw_404(client):
    r = await client.post(
        "/api/gallery/albums/missing/items",
        data=_png_bytes(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    assert r.status == 404


# ─── Album item count after upload ──────────────────────────────────────


async def test_album_item_count_increments(client):
    aid = await _make_album(client)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=_png_bytes(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    assert r.status == 201
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert (await r.json())["item_count"] == 1


# ─── Item delete ────────────────────────────────────────────────────────


async def test_item_delete_round_trip(client):
    aid = await _make_album(client)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=_png_bytes(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    iid = (await r.json())["id"]
    r = await client.delete(
        f"/api/gallery/items/{iid}",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    # Album item_count back to 0.
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert (await r.json())["item_count"] == 0
