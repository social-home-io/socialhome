"""Tests for the public SSR pages (§24.7 / §24.8)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


# ─── Landing page ────────────────────────────────────────────────────


async def test_landing_renders_server_name(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "My Global Server" in text
    # QR img tag in Connect section.
    assert 'src="data:image/png;base64,' in text


async def test_landing_renders_updated_server_name_from_db(client):
    app = client._app
    await app[gfs_admin_repo_key].set_config(
        "server_name",
        "Pascal's GFS",
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Pascal&#x27;s GFS" in text or "Pascal's GFS" in text


async def test_landing_lists_active_spaces_only(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-active",
            owning_instance="o.home",
            name="Active Space",
            description="hello",
            accent_color="#ff0000",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending Space",
            status="pending",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Active Space" in text
    assert "Pending Space" not in text


async def test_landing_search_filters(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-makers",
            owning_instance="o.home",
            name="Makers Space",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-garden",
            owning_instance="o.home",
            name="Garden Club",
            status="active",
        )
    )
    resp = await client.get("/?search=makers")
    text = await resp.text()
    assert "Makers Space" in text
    assert "Garden Club" not in text


async def test_landing_audience_filter(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="family-sp",
            owning_instance="o.home",
            name="Family",
            status="active",
            min_age=0,
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="adult-sp",
            owning_instance="o.home",
            name="Adult Only",
            status="active",
            min_age=18,
        )
    )
    # Family filter hides the adult space.
    resp = await client.get("/?audience=family")
    text = await resp.text()
    assert "Family" in text
    assert "Adult Only" not in text
    # Adult filter hides the family space.
    resp = await client.get("/?audience=adult")
    text = await resp.text()
    assert "Adult Only" in text


async def test_landing_listing_rate_limit(client):
    """Spec §24.7.3: 30 GETs/min/IP on the public listing."""
    for _ in range(30):
        resp = await client.get("/")
        assert resp.status == 200
    resp = await client.get("/")
    assert resp.status == 429


# ─── Space page ───────────────────────────────────────────────────────


async def test_space_page_renders_deep_link(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-deep",
            owning_instance="o.home",
            name="Deep Link Space",
            description="about",
            accent_color="#112233",
            status="active",
        )
    )
    resp = await client.get("/spaces/sp-deep")
    assert resp.status == 200
    text = await resp.text()
    assert "sh://join-space/http://gfs.test/spaces/sp-deep" in text
    assert 'property="og:title"' in text


async def test_space_page_404_for_pending_or_banned(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending",
            status="pending",
        )
    )
    resp = await client.get("/spaces/sp-pending")
    assert resp.status == 404


# ─── Invite page ──────────────────────────────────────────────────────


async def test_invite_page_known_token(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    admin_repo = app[gfs_admin_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="inv-sp",
            owning_instance="o.home",
            name="Invite Me",
            accent_color="#aabbcc",
            status="active",
        )
    )
    # Seed a valid invite-token row.
    await admin_repo._db.enqueue(
        "INSERT INTO gfs_invite_tokens(gfs_token, space_id, "
        "source_instance_id, max_uses) VALUES(?, ?, ?, ?)",
        ("invtok-1", "inv-sp", "o.home", 5),
    )
    resp = await client.get("/join/invtok-1")
    assert resp.status == 200
    text = await resp.text()
    assert "Invite Me" in text
    assert "sh://gfs-invite/http://gfs.test/join/invtok-1" in text


async def test_invite_page_unknown_token_404(client):
    resp = await client.get("/join/no-such-token")
    assert resp.status == 404


# ─── Pairing token rate-limit (spec §24.7.4) ──────────────────────────


async def test_pairing_token_rate_limit_per_ip(client):
    """A fresh token is issued on first visit; second visit within the
    30-second window gets a ``please-wait`` placeholder token instead.
    """
    resp = await client.get("/")
    assert resp.status == 200
    text_a = await resp.text()
    # Second immediate visit — rate-limited.
    resp = await client.get("/")
    text_b = await resp.text()
    assert "please-wait" in text_b
    # (The first token was real.)
    assert "please-wait" not in text_a
