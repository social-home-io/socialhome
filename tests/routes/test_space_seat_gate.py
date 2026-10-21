"""The space seat gate (:mod:`socialhome.routes.space_seat_gate`): no
``/api/spaces/{id}/…`` route tells a caller with no seat whether a space
id exists.

Regression: admin-gated routes (bans, compat, invite tokens, join-request
queue, GFS servers, archive, sync, moderation, reports, …) looked the
space up first — an unknown id answered ``404`` while a real space the
caller had no seat in answered ``403`` — an existence oracle for every
local user.
"""

import re

import pytest

from socialhome.app_keys import (
    media_signer_key,
    profile_picture_repo_key,
    space_cover_repo_key,
    space_icon_repo_key,
)
from socialhome.routes.space_seat_gate import SEATLESS_ROUTES

from .conftest import _auth
from .test_space_read_gate import _add_user, _seat, _seed_space

_UNKNOWN = "0" * 32
_PRIVATE = "sp-priv"


@pytest.fixture
async def seeded(client):
    """A private space owned by olive; guest is a local user in no space."""
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "guest": await _add_user(client, "guest", "uid-guest"),
    }
    await _seed_space(client, _PRIVATE, "private")
    await _seat(client, _PRIVATE, "uid-olive", "owner")
    return client, hdr


def _space_routes(app) -> list[tuple[str, str]]:
    """Every ``(METHOD, canonical)`` under ``/api/spaces/{id|space_id}``
    that a view implements."""
    out: set[tuple[str, str]] = set()
    for resource in app.router.resources():
        canonical = resource.canonical
        if not re.match(r"^/api/spaces/\{(id|space_id)\}", canonical):
            continue
        for route in resource:
            view = route.handler
            for method in ("get", "post", "put", "patch", "delete"):
                if any(method in vars(c) for c in getattr(view, "__mro__", ())[:-1]):
                    if method == "get" and canonical.endswith("export.ics"):
                        continue  # token-authorized public path
                    out.add((method.upper(), canonical))
    return sorted(out)


def _url(canonical: str, space_id: str) -> str:
    first = canonical.split("/")[3]
    return re.sub(r"\{[a-z_]+\}", "x1", canonical.replace(first, space_id, 1))


async def _answer(client, method: str, url: str, headers: dict) -> tuple[int, str]:
    r = await client.request(method, url, json={}, headers=headers)
    return r.status, await r.text()


async def test_no_space_route_is_an_existence_oracle(seeded):
    """A real private space and an unknown id answer every space route the
    same — status AND body — for a local user with no seat."""
    client, hdr = seeded
    routes = _space_routes(client.app)
    assert len(routes) > 100  # the whole surface, not a sample
    leaks = []
    for method, canonical in routes:
        real = await _answer(client, method, _url(canonical, _PRIVATE), hdr["guest"])
        unknown = await _answer(client, method, _url(canonical, _UNKNOWN), hdr["guest"])
        if real != unknown:
            leaks.append((method, canonical, real, unknown))
    assert leaks == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/bans"),
        ("GET", "/compat"),
        ("GET", "/invite-tokens"),
        ("GET", "/join-requests"),
        ("PATCH", ""),
        ("POST", "/archive"),
        ("GET", "/moderation"),
        ("GET", "/reports"),
        ("GET", "/gfs-servers"),
    ],
)
async def test_no_seat_gets_the_read_gates_403(seeded, method, path):
    client, hdr = seeded
    for sid in (_PRIVATE, _UNKNOWN):
        r = await client.request(
            method, f"/api/spaces/{sid}{path}", json={}, headers=hdr["guest"]
        )
        assert r.status == 403, (sid, await r.text())
        assert (await r.json())["error"] == {
            "code": "FORBIDDEN",
            "detail": "Not a space member.",
        }


async def test_a_seat_reaches_the_routes_own_check(seeded):
    """A follower holds a seat: the gate lets them through, and the route's
    own admin check answers."""
    client, hdr = seeded
    await _seat(client, _PRIVATE, "uid-guest", "subscriber")
    r = await client.get(f"/api/spaces/{_PRIVATE}/bans", headers=hdr["guest"])
    assert r.status == 403
    assert (await r.json())["error"]["detail"] == "admin or owner required"


async def test_the_owner_passes(seeded):
    client, hdr = seeded
    r = await client.get(f"/api/spaces/{_PRIVATE}/bans", headers=hdr["owner"])
    assert r.status == 200, await r.text()


async def test_a_household_admin_passes_without_a_seat(seeded):
    """The operator lists every space anyway (``/api/admin/spaces``); the
    route's own checks still apply."""
    client, _hdr = seeded
    r = await client.get(f"/api/spaces/{_PRIVATE}/bans", headers=_auth(client._tok))
    assert r.status == 403
    assert (await r.json())["error"]["detail"] == "admin or owner required"


async def test_a_signed_media_url_is_a_capability(seeded):
    """A signed cover URL needs no seat — the signature is the grant."""
    client, _hdr = seeded
    await client.app[space_cover_repo_key].set(
        _PRIVATE, bytes_webp=b"RIFF0000WEBP", hash="h1", width=1, height=1
    )
    signer = client.app[media_signer_key]
    r = await client.get(signer.sign(f"/api/spaces/{_PRIVATE}/cover"))
    assert r.status == 200, await r.text()


@pytest.mark.parametrize(("method", "canonical"), sorted(SEATLESS_ROUTES))
async def test_seatless_routes_are_real_routes(client, method, canonical):
    assert (method, canonical) in _space_routes(client.app)


_WEBP = b"RIFF0000WEBP"


async def _seed_space_pictures(client) -> None:
    """A member picture of the owner, a cover and an icon on the private
    space."""
    pic = {"bytes_webp": _WEBP, "hash": "h1", "width": 1, "height": 1}
    await client.app[profile_picture_repo_key].set_member_picture(
        _PRIVATE, "uid-olive", **pic
    )
    await client.app[space_cover_repo_key].set(_PRIVATE, **pic)
    await client.app[space_icon_repo_key].set(_PRIVATE, **pic)


_PICTURE_PATHS = ("/members/uid-olive/picture", "/cover", "/icon")


@pytest.mark.parametrize("path", _PICTURE_PATHS)
async def test_a_household_admin_without_a_seat_gets_no_space_picture(seeded, path):
    """Regression: the seat gate lets a household admin through without a
    seat, so a space's pictures run the read gate themselves — the
    operator lists every space but sees none of its pictures."""
    client, _hdr = seeded
    await _seed_space_pictures(client)
    r = await client.get(f"/api/spaces/{_PRIVATE}{path}", headers=_auth(client._tok))
    assert r.status == 403, await r.text()
    assert (await r.json())["error"] == {
        "code": "FORBIDDEN",
        "detail": "Not a space member.",
    }


@pytest.mark.parametrize("path", _PICTURE_PATHS)
async def test_a_reader_gets_the_space_pictures(seeded, path):
    client, hdr = seeded
    await _seed_space_pictures(client)
    r = await client.get(f"/api/spaces/{_PRIVATE}{path}", headers=hdr["owner"])
    assert r.status == 200, await r.text()
    assert await r.read() == _WEBP


@pytest.mark.parametrize("path", _PICTURE_PATHS)
async def test_a_signed_space_picture_url_needs_no_seat(seeded, path):
    client, _hdr = seeded
    await _seed_space_pictures(client)
    signer = client.app[media_signer_key]
    r = await client.get(signer.sign(f"/api/spaces/{_PRIVATE}{path}"))
    assert r.status == 200, await r.text()
    assert await r.read() == _WEBP
