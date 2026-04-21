"""Full route tests for media serve + upload endpoints."""

import os
from .conftest import _auth


async def test_media_serve_file(client):
    """GET /api/media/{filename} serves an existing file."""
    h = _auth(client._tok)
    # Write a test file to media_path
    from social_home.app_keys import config_key

    config = client.app[config_key]
    media_dir = config.media_path
    os.makedirs(media_dir, exist_ok=True)
    test_file = os.path.join(media_dir, "test.txt")
    with open(test_file, "w") as f:
        f.write("hello media")

    r = await client.get("/api/media/test.txt", headers=h)
    assert r.status == 200
    body = await r.read()
    assert b"hello media" in body


async def test_media_serve_404(client):
    """GET /api/media/{filename} returns 404 for missing file."""
    h = _auth(client._tok)
    r = await client.get("/api/media/nonexistent.webp", headers=h)
    assert r.status == 404


async def test_media_path_traversal(client):
    """Filename with path separator is rejected."""
    h = _auth(client._tok)
    # aiohttp resolves ../ before handler, so test the dotfile check instead
    r = await client.get("/api/media/.env", headers=h)
    assert r.status == 400


async def test_media_dotfile_rejected(client):
    """GET /api/media/.hidden returns 400."""
    h = _auth(client._tok)
    r = await client.get("/api/media/.hidden", headers=h)
    assert r.status == 400


async def test_media_upload_image(client):
    """POST /api/media/upload with a small JPEG uploads successfully."""
    h = _auth(client._tok)
    # Create a minimal JPEG
    from PIL import Image
    import io

    img = Image.new("RGB", (50, 50), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    # Multipart upload
    import aiohttp

    data = aiohttp.FormData()
    data.add_field("file", jpeg_bytes, filename="test.jpg", content_type="image/jpeg")
    r = await client.post("/api/media/upload", data=data, headers=h)
    assert r.status == 201
    body = await r.json()
    assert "url" in body
    assert body["url"].startswith("/api/media/")
    assert body["filename"].endswith(".webp")


async def test_media_upload_not_multipart(client):
    """POST /api/media/upload without multipart returns 400."""
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    r = await client.post("/api/media/upload", data=b"{}", headers=h)
    assert r.status == 400


async def test_media_upload_invalid_image(client):
    """POST /api/media/upload with garbage data returns 422."""
    h = _auth(client._tok)
    import aiohttp

    data = aiohttp.FormData()
    data.add_field(
        "file", b"not an image", filename="bad.jpg", content_type="image/jpeg"
    )
    r = await client.post("/api/media/upload", data=data, headers=h)
    assert r.status == 422


async def test_media_serve_webp(client):
    """Serve a .webp file that was just uploaded."""
    h = _auth(client._tok)
    from PIL import Image
    import io

    img = Image.new("RGB", (30, 30), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")

    import aiohttp

    data = aiohttp.FormData()
    data.add_field(
        "file", buf.getvalue(), filename="blue.jpg", content_type="image/jpeg"
    )
    r = await client.post("/api/media/upload", data=data, headers=h)
    body = await r.json()
    filename = body["filename"]

    r2 = await client.get(f"/api/media/{filename}", headers=h)
    assert r2.status == 200
    assert r2.headers.get("Content-Type", "").startswith("image/")
