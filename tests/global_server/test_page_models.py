"""The loaders behind the public pages (:mod:`.page_models`)."""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
    gfs_invite_repo_key,
    gfs_moment_public_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.page_models import (
    load_invite_page,
    load_person_page,
    load_space_page,
    server_name,
)
from socialhome.global_server.server import create_gfs_app


@pytest.fixture
async def app(tmp_dir):
    app = create_gfs_app(
        GfsConfig(base_url="http://gfs.test", data_dir=str(tmp_dir), port=0)
    )
    async with TestClient(TestServer(app)):
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="o.home",
                display_name="o",
                public_key="ab" * 32,
                status="active",
            )
        )
        yield app


async def _space(app, **kw):
    fields = {
        "space_id": "sp",
        "owning_instance": "o.home",
        "name": "Garden",
        "status": "active",
    }
    fields.update(kw)
    await app[gfs_fed_repo_key].upsert_space(GlobalSpace(**fields))


async def test_server_name_prefers_the_admin_override(app):
    assert await server_name(app) == "My Global Server"
    await app[gfs_admin_repo_key].set_config("server_name", "Bern")
    assert await server_name(app) == "Bern"


async def test_space_page_only_for_a_listed_space(app):
    assert await load_space_page(app, "sp") is None
    await _space(app)
    page = await load_space_page(app, "sp")
    assert page is not None and page["name"] == "Garden"
    await _space(app, withdrawn=True)
    assert await load_space_page(app, "sp") is None
    await _space(app, status="banned")
    assert await load_space_page(app, "sp") is None


async def test_invite_page_needs_a_live_token_and_a_listed_space(app):
    await _space(app, status="pending")
    now = int(time.time())
    await app[gfs_invite_repo_key].create(
        gfs_token="tok",
        space_id="sp",
        source_instance_id="o.home",
        blob="QUJD",
        created_at=now,
        expires_at=now + 60,
    )
    assert await load_invite_page(app, "tok") is None  # space not listed yet
    await _space(app)
    page = await load_invite_page(app, "tok")
    assert page is not None
    assert page["invite_code"] == "socialhome://invite#QUJD"
    assert await load_invite_page(app, "other") is None


async def test_person_page_only_for_an_active_registration(app):
    assert await load_person_page(app, "u") is None
    await app[gfs_moment_public_registry_key].register_user(
        user_id="u",
        instance_id="o.home",
        username="noor",
        display_name="Noor",
        home_instance_pk="ab" * 32,
    )
    page = await load_person_page(app, "u")
    assert page is not None
    assert page.registration.display_name == "Noor"
    assert page.follower_count == 0
