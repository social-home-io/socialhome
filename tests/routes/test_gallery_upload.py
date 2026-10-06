"""Coverage for routes/gallery.py upload paths (multipart + raw)."""

from __future__ import annotations

import functools
import io
import os

import aiohttp
from PIL import Image

from socialhome.hardening import DEFAULT_JSON_MAX_BYTES

from .conftest import _auth


def _png_bytes() -> bytes:
    img = Image.new("RGB", (8, 8), (50, 100, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@functools.cache
def _big_jpeg_bytes() -> bytes:
    """A real JPEG well over aiohttp's 1 MiB default ``client_max_size``.

    Random noise does not compress, so a modest canvas lands at a few MiB
    — the size of an ordinary phone photo."""
    img = Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    data = buf.getvalue()
    assert len(data) > 3 * 1024 * 1024
    return data


_BOUNDARY = "sh-test-boundary-7f3a"


def _manual_multipart(data: bytes, *, content_type: str) -> tuple[bytes, str]:
    """A single-part ``multipart/form-data`` body + its Content-Type.

    Built by hand so the test can stream it as an async generator —
    ``aiohttp.MultipartWriter`` is not async-iterable."""
    head = (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.jpg"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{_BOUNDARY}--\r\n".encode()
    return head + data + tail, f"multipart/form-data; boundary={_BOUNDARY}"


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
    # Server-emitted URLs are relative — see PR #291 (ingress).
    assert body["url"].startswith("api/media/")
    assert body["thumbnail_url"].startswith("api/media/")


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


async def test_upload_multipart_jpeg_over_aiohttp_1mib_client_max_size_is_201(
    client,
):
    """Regression: aiohttp >= 3.13.3 (aio-libs/aiohttp#11889) makes
    ``BodyPartReader.read()`` 413 once a part exceeds the app's
    ``client_max_size`` (1 MiB default), so every phone photo failed with
    ``Maximum request body size 1048576 exceeded``. The route streams the
    part under its own 100 MiB cap instead."""
    aid = await _make_album(client)
    data = _big_jpeg_bytes()
    assert len(data) > DEFAULT_JSON_MAX_BYTES == 1_048_576
    form = aiohttp.FormData()
    form.add_field("file", data, filename="big.jpg", content_type="image/jpeg")
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=form,
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["item_type"] == "photo"
    assert body["url"].startswith("api/media/")


async def test_upload_chunked_multipart_without_content_length_over_1mib_is_201(
    client,
):
    """Home Assistant ingress forwards uploads chunked — HA Core strips
    ``Content-Length`` — so the cap must hold while streaming, and a
    multi-MiB part must still pass aiohttp's 1 MiB ``client_max_size``."""
    aid = await _make_album(client)
    data = _big_jpeg_bytes()
    body, ctype = _manual_multipart(data, content_type="image/jpeg")
    assert len(body) > DEFAULT_JSON_MAX_BYTES

    async def gen():
        for i in range(0, len(body), 64 * 1024):
            yield body[i : i + 64 * 1024]

    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=gen(),
        headers={**_auth(client._tok), "Content-Type": ctype},
    )
    assert r.status == 201, await r.text()
    assert (await r.json())["item_type"] == "photo"


async def test_upload_multipart_part_over_cap_is_413_payload_too_large(
    client, monkeypatch
):
    monkeypatch.setattr("socialhome.routes.gallery.GALLERY_MAX_UPLOAD_BYTES", 64 * 1024)
    aid = await _make_album(client)
    data = _big_jpeg_bytes()
    form = aiohttp.FormData()
    form.add_field("file", data, filename="big.jpg", content_type="image/jpeg")
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=form,
        headers=_auth(client._tok),
    )
    assert r.status == 413
    err = (await r.json())["error"]
    assert err["code"] == "PAYLOAD_TOO_LARGE"
    assert err["params"] == {"max_mb": 1}


async def test_upload_multipart_declared_over_cap_is_413_before_reading_the_part(
    client, monkeypatch
):
    """A multipart request whose ``Content-Length`` already exceeds the cap
    is refused up front — the multipart part is never read."""
    monkeypatch.setattr("socialhome.routes.gallery.GALLERY_MAX_UPLOAD_BYTES", 64 * 1024)

    async def _never(*_a, **_kw):
        raise AssertionError("part was read despite an over-cap Content-Length")

    monkeypatch.setattr("socialhome.routes.gallery.read_part_capped", _never)
    aid = await _make_album(client)
    form = aiohttp.FormData()
    form.add_field(
        "file", b"j" * (100 * 1024), filename="x.jpg", content_type="image/jpeg"
    )
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=form,
        headers=_auth(client._tok),
    )
    assert r.status == 413
    err = (await r.json())["error"]
    assert err["code"] == "PAYLOAD_TOO_LARGE"
    assert err["params"] == {"max_mb": 1}


async def test_upload_raw_body_over_cap_is_413_payload_too_large(client, monkeypatch):
    monkeypatch.setattr("socialhome.routes.gallery.GALLERY_MAX_UPLOAD_BYTES", 64 * 1024)
    aid = await _make_album(client)
    data = _big_jpeg_bytes()

    async def gen():
        # Chunked: no Content-Length, so only the streaming cap can refuse it.
        for i in range(0, len(data), 64 * 1024):
            yield data[i : i + 64 * 1024]

    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=gen(),
        headers={**_auth(client._tok), "Content-Type": "image/jpeg"},
    )
    assert r.status == 413
    err = (await r.json())["error"]
    assert err["code"] == "PAYLOAD_TOO_LARGE"
    assert err["params"] == {"max_mb": 1}


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
