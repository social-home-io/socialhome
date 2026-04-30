"""Tests for socialhome.routes.media — including signed-URL auth."""

import pathlib

import pytest

from socialhome.app_keys import config_key, media_signer_key

from .conftest import _auth


@pytest.fixture
async def media_file(client):
    """Drop a tiny WebP-shaped blob into the media dir under a known
    filename so ``GET /api/media/<name>`` can stream it back.

    Returns the canonical (unsigned) URL.
    """
    cfg = client.app[config_key]
    media_dir = pathlib.Path(cfg.media_path)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "abc.webp").write_bytes(b"\x52\x49\x46\x46\x00\x00\x00\x00WEBPVP8 ")
    return "/api/media/abc.webp"


async def test_get_nonexistent_media_404(client):
    """GET /api/media/nonexistent returns 404 when authed."""
    r = await client.get("/api/media/nonexistent.webp", headers=_auth(client._tok))
    assert r.status == 404


async def test_get_with_bearer_token_succeeds(client, media_file):
    """Bearer auth still works for media — fetch() callers rely on it."""
    r = await client.get(media_file, headers=_auth(client._tok))
    assert r.status == 200
    body = await r.read()
    assert body.startswith(b"\x52\x49\x46\x46")  # "RIFF"


async def test_get_with_valid_signature_succeeds(client, media_file):
    """Signed URL authenticates without any Authorization header — this
    is the path browsers use for ``<img src>`` etc."""
    signer = client.app[media_signer_key]
    signed = signer.sign(media_file)
    r = await client.get(signed)  # no Authorization header
    assert r.status == 200


async def test_get_with_tampered_signature_401(client, media_file):
    """Flipping a single character of the sig causes auth to fail."""
    signer = client.app[media_signer_key]
    signed = signer.sign(media_file)
    # Mutate the last char of the sig so HMAC compare fails.
    if signed.endswith("A"):
        tampered = signed[:-1] + "B"
    else:
        tampered = signed[:-1] + "A"
    r = await client.get(tampered)
    assert r.status == 401


async def test_get_with_expired_signature_401(client, media_file):
    """A URL whose ``exp`` is in the past returns 401."""
    signer = client.app[media_signer_key]
    # Sign with a 1-second TTL using ``now`` far in the past.
    signed = signer.sign(media_file, ttl=1, now=1)
    r = await client.get(signed)
    assert r.status == 401


async def test_get_without_any_auth_401(client, media_file):
    """Plain canonical URL with no auth headers and no sig → 401."""
    r = await client.get(media_file)
    assert r.status == 401


async def test_signed_url_for_user_picture(client):
    """Same scheme works against ``/api/users/{id}/picture`` —
    avatars rely on it. We don't need a real picture row; the
    auth-strategy decision happens before the route handler runs.
    Without a stored picture the route returns 404, so we just assert
    that the response is *not* 401 (i.e. signed-URL auth succeeded)."""
    signer = client.app[media_signer_key]
    canonical = f"/api/users/{client._uid}/picture"
    signed = signer.sign(canonical)
    r = await client.get(signed)
    # 404 (no picture set) or 200 (rare) both prove auth passed.
    assert r.status != 401
