"""The public pages in the pinned viewer: hand-off pages + their JSON.

``/spaces/{id}``, ``/join/{token}`` and ``/moments/{user}`` answer a
link-preview page that refreshes to the configured viewer; ``/gfs/pages/…`` serves what the viewer renders,
cross-origin, with images as ``data:`` URIs only.
"""

from __future__ import annotations

import base64
import html
import re
import time
from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
    gfs_invite_repo_key,
    gfs_moment_public_registry_key,
    gfs_user_picture_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GfsUserPicture, GlobalSpace
from socialhome.global_server.public import LISTING_MAX_PER_MINUTE
from socialhome.global_server.routes.viewer_pages import (
    INVITE_BODY_MAX_BYTES,
    image_data_uri,
)
from socialhome.global_server.server import create_gfs_app
from socialhome.pinned_viewer import load_pinned_viewer

from ..conftest import assert_strict_public_page

_VIEWER = "https://viewer.example.org"
_GFS = "https://gfs.example.org"
_PNG = "data:image/png;base64,iVBORw0KGgo="


def _config(tmp_dir, **kw) -> GfsConfig:
    base = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url=_GFS,
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
        public_viewer_base_url=_VIEWER,
    )
    return replace(base, **kw)


async def _client(tmp_dir, **kw):
    app = create_gfs_app(_config(tmp_dir, **kw))
    tc = TestClient(TestServer(app))
    await tc.start_server()
    tc._app = app  # type: ignore[attr-defined]
    return tc


@pytest.fixture
async def client(tmp_dir):
    tc = await _client(tmp_dir)
    try:
        yield tc
    finally:
        await tc.close()


@pytest.fixture
async def ssr_client(tmp_dir):
    """A GFS without a viewer: the pages render here, as before."""
    tc = await _client(tmp_dir, public_viewer_base_url="")
    try:
        yield tc
    finally:
        await tc.close()


async def _household(app, instance_id: str) -> None:
    await app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key="ab" * 32,
            status="active",
        )
    )


async def _space(app, **kw) -> None:
    await _household(app, "o.home")
    fields = {
        "space_id": "sp-garden",
        "owning_instance": "o.home",
        "name": "Garden Club",
        "description": "Tomatoes and swaps",
        "about_markdown": "**Fridays** at the allotment",
        "cover_url": _PNG,
        "icon_url": "https://tracker.example.com/icon.png",
        "accent_color": "#3a7d44",
        "primary_color": "red; background:url(x)",
        "status": "active",
        "category": "hobby_crafts",
        "subscriber_count": 12,
    }
    fields.update(kw)
    await app[gfs_fed_repo_key].upsert_space(GlobalSpace(**fields))


async def _invite(app, token="tok_abc-123", *, expires_in=3600) -> str:
    now = int(time.time())
    await app[gfs_invite_repo_key].create(
        gfs_token=token,
        space_id="sp-garden",
        source_instance_id="o.home",
        blob="eyJ0IjoxfQ",
        created_at=now,
        expires_at=now + expires_in,
    )
    return token


async def _person(app, *, picture: bool = True) -> None:
    await _household(app, "inst-noor")
    registry = app[gfs_moment_public_registry_key]
    await registry.register_user(
        user_id="u-noor",
        instance_id="inst-noor",
        username="noor",
        display_name="Noor Haddad",
        home_instance_pk="ab" * 32,
        bio="Beekeeper on a rooftop.",
        picture_digest="d1" if picture else None,
    )
    if picture:
        await app[gfs_user_picture_repo_key].upsert(
            GfsUserPicture(
                user_id="u-noor",
                bytes_=b"RIFFwebp",
                mime="image/webp",
                digest="d1",
                updated_at=0,
            )
        )


def _fragment(location: str) -> tuple[str, dict[str, str]]:
    page, _, fragment = location.partition("#")
    return page, dict(parse_qsl(fragment))


_REFRESH_RE = re.compile(r'<meta http-equiv="refresh" content="0;url=([^"]+)">')


async def _handoff_target(resp) -> str:
    """The viewer URL the hand-off page refreshes to (HTML-unescaped)."""
    match = _REFRESH_RE.search(await resp.text())
    assert match, "hand-off page without a refresh"
    return html.unescape(match.group(1))


# ─── Hand-off pages (link preview + refresh) ──────────────────────────────


@pytest.mark.parametrize(
    ("path", "view", "key", "value"),
    [
        ("/spaces/sp-garden", "space", "s", "sp-garden"),
        ("/join/tok_abc-123", "join", "t", "tok_abc-123"),
        ("/moments/u-noor", "person", "u", "u-noor"),
    ],
)
async def test_a_public_page_hands_off_to_the_pinned_viewer(
    client, path, view, key, value
):
    resp = await client.get(path, allow_redirects=False)
    assert resp.status == 200
    assert "Location" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")
    page, params = _fragment(await _handoff_target(resp))
    manifest = load_pinned_viewer()
    assert manifest is not None
    assert page == f"{_VIEWER}/{manifest.path}"
    assert params == {"v": view, "g": _GFS, key: value}


async def test_the_handoff_names_no_token_outside_the_fragment(client):
    resp = await client.get("/join/tok_abc-123", allow_redirects=False)
    page, _ = _fragment(await _handoff_target(resp))
    assert "tok_abc-123" not in page
    text = await resp.text()
    # The token appears only inside the viewer link (refresh + plain link).
    assert text.count("tok_abc-123") == 2


async def test_every_invite_token_gets_the_same_handoff(client):
    """Live, expired or never issued: the hand-off looks the token up not at
    all, so apart from echoing the token it is byte-for-byte the same."""
    await _space(client._app)
    live = await _invite(client._app, token="tok_live_0001")
    texts = []
    for token in (live, "tok_none_0001"):
        resp = await client.get(f"/join/{token}", allow_redirects=False)
        assert resp.status == 200
        assert _fragment(await _handoff_target(resp))[1]["t"] == token
        texts.append((await resp.text()).replace(token, "TOKEN"))
    assert texts[0] == texts[1]
    # Generic tags only: nothing about the space the invite is for.
    assert "Garden Club" not in texts[0]
    assert "og:image" not in texts[0]


async def test_the_space_handoff_carries_its_preview(client):
    await _space(client._app)
    await client._app[gfs_admin_repo_key].set_config("server_name", "Bern GFS")
    resp = await client.get("/spaces/sp-garden", allow_redirects=False)
    text = await resp.text()
    assert '<meta property="og:title" content="Garden Club">' in text
    assert '<meta property="og:description" content="Tomatoes and swaps">' in text
    assert '<meta property="og:site_name" content="Bern GFS">' in text
    assert '<meta name="twitter:card" content="summary_large_image">' in text
    image = re.search(r'<meta property="og:image" content="([^"]+)">', text)
    assert image is not None
    url = html.unescape(image.group(1))
    assert url.startswith(f"{_GFS}/gfs/pages/spaces/sp-garden/image?v=")
    # The image URL is one this GFS serves anonymously.
    img = await client.get(urlsplit(url).path + "?" + urlsplit(url).query)
    assert img.status == 200
    assert img.headers["Content-Type"] == "image/png"
    assert await img.read() == base64.b64decode(_PNG.split(",", 1)[1])


async def test_the_space_handoff_escapes_owner_text(client):
    await _space(
        client._app,
        name='"><script>alert(1)</script>',
        description="a & b <i>",
        cover_url="https://tracker.example.com/c.png",
    )
    resp = await client.get("/spaces/sp-garden", allow_redirects=False)
    text = await resp.text()
    assert "<script" not in text
    assert "&quot;&gt;&lt;script&gt;" in text
    assert "a &amp; b &lt;i&gt;" in text
    # An https cover/icon is never handed to a crawler to fetch.
    assert "og:image" not in text
    assert "tracker.example.com" not in text


async def test_an_unlisted_space_hands_off_with_generic_tags(client):
    await _space(client._app, status="pending")
    resp = await client.get("/spaces/sp-garden", allow_redirects=False)
    assert resp.status == 200
    text = await resp.text()
    assert "Garden Club" not in text
    assert "og:image" not in text
    img = await client.get("/gfs/pages/spaces/sp-garden/image")
    assert img.status == 404


async def test_a_broken_stored_image_is_a_404_not_a_crash(client):
    await _space(client._app, cover_url="data:image/png;base64,iVBORw0KGgo")
    img = await client.get("/gfs/pages/spaces/sp-garden/image")
    assert img.status == 404


async def test_the_person_handoff_carries_name_bio_and_picture(client):
    await _person(client._app)
    resp = await client.get("/moments/u-noor", allow_redirects=False)
    text = await resp.text()
    assert '<meta property="og:title" content="Noor Haddad">' in text
    assert "Beekeeper on a rooftop." in text
    assert '<meta property="og:type" content="profile">' in text
    assert '<meta name="twitter:card" content="summary">' in text
    assert (
        f'<meta property="og:image" content="{_GFS}/gfs/moments/users/u-noor/'
        'picture?v=d1">'
    ) in text


async def test_an_inactive_person_shows_nothing_of_theirs(client):
    await _person(client._app)
    await client._app[gfs_moment_public_registry_key].deregister_user("u-noor")
    resp = await client.get("/moments/u-noor", allow_redirects=False)
    assert resp.status == 200
    text = await resp.text()
    assert "Noor" not in text
    assert "Beekeeper" not in text
    assert "og:image" not in text


@pytest.mark.security
@pytest.mark.parametrize(
    "path",
    [
        "/spaces/a%20b",
        "/spaces/%2F%2Fevil.example",
        "/spaces/x%23g%3Dhttps%3A%2F%2Fevil.example",
        "/join/" + "a" * 257,
        "/moments/%3Cscript%3E",
    ],
)
async def test_an_id_the_viewer_would_refuse_renders_here_instead(client, path):
    """Never an open redirect: an id outside the viewer's alphabet is not
    forwarded at all — the page renders (or 404s) on this server."""
    resp = await client.get(path, allow_redirects=False)
    assert resp.status in (200, 404)
    assert "Location" not in resp.headers
    assert "http-equiv" not in await resp.text()


@pytest.mark.security
async def test_the_handoff_target_never_comes_from_the_request(client):
    resp = await client.get(
        "/spaces/sp-garden",
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
        allow_redirects=False,
    )
    location = await _handoff_target(resp)
    assert urlsplit(location).netloc == "viewer.example.org"
    assert _fragment(location)[1]["g"] == _GFS


@pytest.mark.parametrize(
    "kw",
    [
        {"public_viewer_base_url": ""},
        {"public_viewer_base_url": "http://viewer.example.org"},
        {"public_viewer_base_url": "javascript:alert(1)"},
        {"base_url": "http://gfs.example.org"},
    ],
)
async def test_without_a_usable_viewer_the_pages_render_here(tmp_dir, kw):
    tc = await _client(tmp_dir, **kw)
    try:
        await _space(tc._app)
        resp = await tc.get("/spaces/sp-garden", allow_redirects=False)
        assert resp.status == 200
        assert "Garden Club" in await resp.text()
    finally:
        await tc.close()


async def test_the_landing_always_renders_here(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 200


async def test_the_fallback_pages_still_render(ssr_client):
    app = ssr_client._app
    await _space(app)
    token = await _invite(app)
    await _person(app)
    for path, needle in (
        ("/spaces/sp-garden", "Garden Club"),
        (f"/join/{token}", "socialhome://invite#eyJ0IjoxfQ"),
        ("/moments/u-noor", "Noor Haddad"),
    ):
        resp = await ssr_client.get(path, allow_redirects=False)
        assert resp.status == 200, path
        text = await resp.text()
        assert needle in text
        assert_strict_public_page(resp, text)
    resp = await ssr_client.get("/join/nope", allow_redirects=False)
    assert resp.status == 404


# ─── Space page JSON ──────────────────────────────────────────────────────


async def test_space_page_json_carries_what_the_page_shows(client):
    await _space(client._app)
    await client._app[gfs_admin_repo_key].set_config("server_name", "Bern GFS")
    resp = await client.get("/gfs/pages/spaces/sp-garden")
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in resp.headers
    body = await resp.json()
    assert body == {
        "server_name": "Bern GFS",
        "space": {
            "space_id": "sp-garden",
            "name": "Garden Club",
            "description": "Tomatoes and swaps",
            "about_markdown": "**Fridays** at the allotment",
            "cover": _PNG,
            # An https icon is dropped, never handed on for the viewer to fetch.
            "icon": None,
            # Owner colours pass only as plain hex.
            "primary_color": "#3a7d44",
            "accent_color": "#3a7d44",
            "member_count": 12,
            "category": "hobby_crafts",
        },
    }


async def test_space_page_json_bounds_the_about_text(client):
    await _space(client._app, about_markdown="x" * 10_000, category="nonsense")
    body = await (await client.get("/gfs/pages/spaces/sp-garden")).json()
    assert len(body["space"]["about_markdown"]) == 4000
    assert body["space"]["category"] == "general"


@pytest.mark.parametrize(
    "kw", [{"status": "pending"}, {"withdrawn": True}, {"space_id": "other"}]
)
async def test_space_page_json_is_one_404_for_anything_unlisted(client, kw):
    await _space(client._app, **kw)
    resp = await client.get("/gfs/pages/spaces/sp-garden")
    assert resp.status == 404
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert await resp.json() == {"server_name": "My Global Server"}


# ─── Invite JSON ──────────────────────────────────────────────────────────


async def test_invite_json_hands_over_the_code_and_its_qr(client):
    await _space(client._app, icon_url=_PNG)
    token = await _invite(client._app)
    resp = await client.post("/gfs/pages/invite", json={"token": token})
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    body = await resp.json()
    assert body["server_name"] == "My Global Server"
    assert body["space"] == {
        "name": "Garden Club",
        "accent_color": "#3a7d44",
        "icon": _PNG,
    }
    assert body["invite_code"] == "socialhome://invite#eyJ0IjoxfQ"
    assert body["qr"].startswith("data:image/png;base64,")
    base64.b64decode(body["qr"].split(",", 1)[1], validate=True)


@pytest.mark.parametrize("case", ["unknown", "expired", "unlisted"])
async def test_invite_json_is_one_404_for_every_dead_link(client, case):
    await _space(client._app, status="pending" if case == "unlisted" else "active")
    token = await _invite(client._app, expires_in=-60 if case == "expired" else 3600)
    if case == "unknown":
        token = "never-issued"
    resp = await client.post("/gfs/pages/invite", json={"token": token})
    assert resp.status == 404
    assert await resp.json() == {"server_name": "My Global Server"}


@pytest.mark.parametrize(
    "body", [{}, {"token": ""}, {"token": 5}, {"token": "a" * 257}]
)
async def test_invite_json_refuses_a_malformed_body(client, body):
    resp = await client.post("/gfs/pages/invite", json=body)
    assert resp.status == 400


async def test_invite_json_body_is_bounded(client):
    resp = await client.post(
        "/gfs/pages/invite",
        data=b'{"token":"' + b"a" * INVITE_BODY_MAX_BYTES + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_invite_json_writes_nothing(client):
    """A pure read, like ``GET /join``: the row is unchanged afterwards."""
    await _space(client._app)
    token = await _invite(client._app)
    repo = client._app[gfs_invite_repo_key]
    before = await repo.get_live(token, now=int(time.time()))
    await client.post("/gfs/pages/invite", json={"token": token})
    assert await repo.get_live(token, now=int(time.time())) == before


async def test_invite_json_preflight_allows_the_json_post(client):
    resp = await client.options(
        "/gfs/pages/invite",
        headers={
            "Origin": _VIEWER,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in resp.headers


# ─── Person JSON ──────────────────────────────────────────────────────────


async def test_person_json_carries_the_profile_and_picture(client):
    await _person(client._app)
    resp = await client.get("/gfs/pages/people/u-noor")
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    body = await resp.json()
    assert body == {
        "server_name": "My Global Server",
        "gfs_id": "gfs-test",
        "person": {
            "user_id": "u-noor",
            "username": "noor",
            "display_name": "Noor Haddad",
            "instance_id": "inst-noor",
            "bio": "Beekeeper on a rooftop.",
            "picture": "data:image/webp;base64,"
            + base64.b64encode(b"RIFFwebp").decode(),
            "follower_count": 0,
        },
    }


async def test_person_json_without_a_picture(client):
    await _person(client._app, picture=False)
    body = await (await client.get("/gfs/pages/people/u-noor")).json()
    assert body["person"]["picture"] is None


async def test_person_json_404s_an_unknown_person(client):
    resp = await client.get("/gfs/pages/people/nobody")
    assert resp.status == 404
    assert await resp.json() == {"server_name": "My Global Server"}


# ─── Shared rules ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/gfs/info"),
        ("GET", "/gfs/spaces/sp-garden"),
        ("GET", "/gfs/moments/users/u-noor"),
        ("GET", "/gfs/pages/spaces/a/b"),
    ],
)
async def test_no_cors_beyond_the_page_routes(client, method, path):
    resp = await client.request(method, path, allow_redirects=False)
    assert "Access-Control-Allow-Origin" not in resp.headers


async def test_the_page_json_shares_the_pages_rate_limit(client):
    statuses = [
        (
            await client.get(
                "/gfs/pages/spaces/x", headers={"X-Forwarded-For": "198.51.100.9"}
            )
        ).status
        for _ in range(LISTING_MAX_PER_MINUTE + 1)
    ]
    assert statuses[-1] == 429
    assert set(statuses[:-1]) == {404}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_PNG, _PNG),
        ("data:image/jpeg;base64,/9j/4AAQ", "data:image/jpeg;base64,/9j/4AAQ"),
        ("data:image/svg+xml;base64,PHN2Zz4=", None),
        ("data:text/html;base64,PGI+", None),
        ("https://example.com/a.png", None),
        ('data:image/png;base64,AA" onerror="x', None),
        (None, None),
        ("", None),
    ],
)
def test_only_raster_data_uris_pass(value, expected):
    assert image_data_uri(value) == expected
