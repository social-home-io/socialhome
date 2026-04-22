"""Tests for socialhome.routes.media."""

from .conftest import _auth


async def test_get_nonexistent_media_404(client):
    """GET /api/media/nonexistent returns 404."""
    r = await client.get("/api/media/nonexistent.webp", headers=_auth(client._tok))
    assert r.status == 404
