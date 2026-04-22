"""Tests for socialhome.routes.media (media serving + upload views)."""

from aiohttp import web

from socialhome.routes.media import MediaServeView, MediaUploadView


def test_media_views_are_importable():
    """MediaServeView and MediaUploadView can be imported and mounted."""
    app = web.Application()
    app.router.add_view("/api/media/{filename}", MediaServeView)
    app.router.add_view("/api/media/upload", MediaUploadView)
    routes = {r.resource.canonical for r in app.router.routes()}
    assert "/api/media/{filename}" in routes
    assert "/api/media/upload" in routes
