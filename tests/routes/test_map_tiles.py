"""Route tests for the map tile proxy — /api/map/tiles + /api/map/config.

The tile endpoint is reached by Leaflet's ``<img>`` loads, which cannot
carry ``Authorization: Bearer`` (and under HA ingress the SPA has no
token at all), so the signed-URL capability is the only auth that works.
These tests pin that shape: a valid signature serves the tile, and every
way of not having one (absent, tampered, expired, minted for another
path) is a 401.
"""

import time

import pytest

from socialhome.app_keys import map_tile_service_key, media_signer_key
from socialhome.routes.map_tiles import (
    MAP_ATTRIBUTION,
    TILE_URL_TEMPLATE,
)
from socialhome.services.map_tile_service import (
    MAX_ZOOM,
    Tile,
    TileCoordinateError,
    TileUnavailableError,
)

from .conftest import _auth


class _StubTileService:
    """Boundary stub — no network, records the coordinates asked for."""

    def __init__(self, tile: Tile | None = None, exc: Exception | None = None) -> None:
        self._tile = tile or Tile(body=b"\x89PNG-tile", content_type="image/png")
        self._exc = exc
        self.calls: list[tuple[int, int, int]] = []

    async def fetch(self, z: int, x: int, y: int) -> Tile:
        self.calls.append((z, x, y))
        if self._exc is not None:
            raise self._exc
        return self._tile


@pytest.fixture
def stub(client):
    """Replace the real tile service with a stub for this client."""
    svc = _StubTileService()
    client.app[map_tile_service_key] = svc
    return svc


def _signed_tile_url(client, z: int = 3, x: int = 4, y: int = 5) -> str:
    """Mint a signed tile URL the way ``/api/map/config`` does."""
    signer = client.app[media_signer_key]
    template = signer.sign(TILE_URL_TEMPLATE, ttl=3600)
    return "/" + template.format(z=z, x=x, y=y)


# ── Tile serving ────────────────────────────────────────────────────────


async def test_tile_signed_url_serves_bytes(client, stub):
    """A correctly signed URL returns the tile bytes + caching headers."""
    r = await client.get(_signed_tile_url(client, 3, 4, 5))
    assert r.status == 200
    assert await r.read() == b"\x89PNG-tile"
    assert r.headers["Content-Type"] == "image/png"
    assert r.headers["Cache-Control"] == "public, max-age=604800"
    assert stub.calls == [(3, 4, 5)]


async def test_tile_bearer_token_also_authorises(client, stub):
    """A normal authenticated fetch works too (no signature required)."""
    r = await client.get("/api/map/tiles?z=1&x=0&y=1", headers=_auth(client._tok))
    assert r.status == 200
    assert stub.calls == [(1, 0, 1)]


async def test_tile_without_signature_is_401(client, stub):
    """No ``exp``/``sig`` and no bearer token → unauthorised."""
    r = await client.get("/api/map/tiles?z=3&x=4&y=5")
    assert r.status == 401
    assert stub.calls == []


async def test_tile_tampered_signature_is_401(client, stub):
    """Flipping a character in ``sig`` invalidates the URL."""
    url = _signed_tile_url(client)
    head, sig = url.rsplit("sig=", 1)
    tampered = f"{head}sig={'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    r = await client.get(tampered)
    assert r.status == 401
    assert stub.calls == []


async def test_tile_expired_signature_is_401(client, stub):
    """An ``exp`` in the past is rejected even with a valid HMAC."""
    signer = client.app[media_signer_key]
    template = signer.sign(TILE_URL_TEMPLATE, ttl=-60, now=int(time.time()))
    r = await client.get("/" + template.format(z=2, x=1, y=1))
    assert r.status == 401
    assert stub.calls == []


async def test_tile_signature_for_other_path_is_401(client, stub):
    """A signature minted for another resource does not authorise tiles."""
    signer = client.app[media_signer_key]
    other = signer.sign("api/media/photo.png", ttl=3600)
    query = other.split("?", 1)[1]
    r = await client.get(f"/api/map/tiles?z=2&x=1&y=1&{query}")
    assert r.status == 401
    assert stub.calls == []


@pytest.mark.parametrize(
    "query",
    [
        "z=abc&x=1&y=1",
        "z=1&x=1.5&y=1",
        "z=1&y=1",
        "",
    ],
)
async def test_tile_bad_coordinates_are_400(client, stub, query):
    """Missing or non-integer coordinates never reach the service."""
    r = await client.get(f"/api/map/tiles?{query}", headers=_auth(client._tok))
    assert r.status == 400
    assert (await r.json())["error"]["code"] == "BAD_REQUEST"
    assert stub.calls == []


async def test_tile_out_of_range_coordinates_are_400(client):
    """``TileCoordinateError`` from the service maps to 400."""
    client.app[map_tile_service_key] = _StubTileService(
        exc=TileCoordinateError("zoom out of range: 99"),
    )
    r = await client.get(
        f"/api/map/tiles?z={MAX_ZOOM + 1}&x=0&y=0",
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_tile_upstream_failure_is_502(client):
    """``TileUnavailableError`` maps to 502 — the proxy is the gateway."""
    client.app[map_tile_service_key] = _StubTileService(
        exc=TileUnavailableError("upstream HTTP 403"),
    )
    r = await client.get("/api/map/tiles?z=1&x=0&y=0", headers=_auth(client._tok))
    assert r.status == 502
    assert (await r.json())["error"]["code"] == "TILE_UNAVAILABLE"


# ── Map config ──────────────────────────────────────────────────────────


async def test_map_config_returns_signed_relative_template(client):
    """The SPA gets one signed template it can substitute z/x/y into."""
    r = await client.get("/api/map/config", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()

    url = body["tile_url"]
    assert not url.startswith("/")  # relative — resolves against <base href>
    assert url.startswith("api/map/tiles?")
    for placeholder in ("{z}", "{x}", "{y}"):
        assert placeholder in url
    assert "exp=" in url and "sig=" in url

    # The attribution must reach the SPA byte-identical — it is rendered
    # as HTML by Leaflet and sanitise_for_api must not mangle it.
    assert body["attribution"] == MAP_ATTRIBUTION
    assert body["max_zoom"] == MAX_ZOOM


async def test_map_config_signature_verifies(client):
    """The minted signature actually authorises a tile fetch."""
    client.app[map_tile_service_key] = _StubTileService()
    r = await client.get("/api/map/config", headers=_auth(client._tok))
    url = (await r.json())["tile_url"]

    tile = await client.get("/" + url.format(z=6, x=7, y=8))
    assert tile.status == 200


async def test_map_config_requires_auth(client):
    """Unauthenticated callers get 401 — config is a normal endpoint."""
    r = await client.get("/api/map/config")
    assert r.status == 401


async def test_map_config_fails_closed_without_signer(client):
    """No signer → 503 rather than an unsigned (unusable) tile URL."""
    del client.app[media_signer_key]
    r = await client.get("/api/map/config", headers=_auth(client._tok))
    assert r.status == 503
    assert (await r.json())["error"]["code"] == "SIGNER_UNAVAILABLE"


# ── Rate limiting ───────────────────────────────────────────────────────


async def test_tile_budget_survives_a_full_map_session(client, stub):
    """Panning a map must not trip the 60/min default limit.

    Every Leaflet ``<img>`` authenticates as the constant
    ``SIGNED_URL_PRINCIPAL``, so one bucket is shared by every member of
    the household *and* every map on the page. A desktop viewport is
    ~20 tiles, so the 60/min default died after three viewports and the
    map went grey again — the exact symptom the proxy exists to fix.
    """
    signer = client.app[media_signer_key]
    template = "/" + signer.sign(TILE_URL_TEMPLATE, ttl=3600)

    for i in range(150):
        r = await client.get(template.format(z=10, x=i, y=i))
        assert r.status == 200, f"tile #{i + 1} was rejected with {r.status}"
