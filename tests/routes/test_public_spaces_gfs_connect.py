"""Global spaces follow the household's GFS connections at runtime.

Regression (owner report, 2026-11-22): after connecting a household to a GFS,
Discover stayed empty until Social Home was restarted. The directory poll ran
once at startup and then slept for its hourly interval; connecting a GFS
never woke it. Disconnecting had the mirror bug: the cached listings stayed
(with no GFS left the poll never purged anything at all).

These run the REAL household app against the REAL GFS app, without any
restart in between.
"""

from __future__ import annotations

import asyncio

from socialhome.app_keys import public_space_discovery_key
from socialhome.global_server.app_keys import gfs_admin_repo_key, gfs_fed_repo_key
from socialhome.global_server.domain import ClientInstance, GlobalSpace

from .test_gfs_default_connection import ADMIN, PATH, _gfs, _household, _url

LIST = "/api/public_spaces"


async def _seed_global_space(gfs, space_id: str = "sp-global") -> None:
    fed = gfs.server.app[gfs_fed_repo_key]
    await fed.upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id=space_id,
            owning_instance="owner.home",
            name="Village Garden",
            status="active",
            join_mode="open",
            allow_subscribers=True,
        )
    )


async def _listed(home) -> set[str]:
    r = await home.get(LIST, headers=ADMIN)
    assert r.status == 200, await r.text()
    return {row["space_id"] for row in await r.json()}


async def _eventually(home, want: set[str], *, timeout: float = 5.0) -> set[str]:
    """``GET /api/public_spaces`` once the cache holds exactly *want* (or at
    timeout). Waits on the cache, not the route: the route is rate-limited."""
    cache = home.app[public_space_discovery_key]._repo
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if {lst.space_id for lst in await cache.list_active()} == want:
            break
        await asyncio.sleep(0.05)
    return await _listed(home)


async def test_global_spaces_show_right_after_connecting(aiohttp_client, tmp_dir):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    await _seed_global_space(gfs)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    # Started with no GFS: nothing listed.
    assert await _listed(home) == set()

    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 201, await r.text()
    assert (await r.json())["status"] == "active"

    # No restart: the connect itself refreshes the directory.
    assert await _eventually(home, {"sp-global"}) == {"sp-global"}


async def test_global_spaces_go_right_after_disconnecting(aiohttp_client, tmp_dir):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    await _seed_global_space(gfs)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    conn = await (await home.post(PATH, headers=ADMIN)).json()
    assert await _eventually(home, {"sp-global"}) == {"sp-global"}

    r = await home.delete(f"/api/gfs/connections/{conn['id']}", headers=ADMIN)
    assert r.status in (200, 204), await r.text()

    # The household has no GFS any more: its spaces leave Discover at once.
    assert await _eventually(home, set()) == set()


async def test_a_space_the_gfs_drops_leaves_on_the_next_refresh(
    aiohttp_client, tmp_dir
):
    """A space the GFS stops listing (here: its operator banned it) goes on
    the next refresh, not 24 hours later."""
    gfs = await _gfs(aiohttp_client, tmp_dir)
    await _seed_global_space(gfs, "sp-a")
    await _seed_global_space(gfs, "sp-b")
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    await home.post(PATH, headers=ADMIN)
    assert await _eventually(home, {"sp-a", "sp-b"}) == {"sp-a", "sp-b"}

    await gfs.server.app[gfs_fed_repo_key].upsert_space(
        GlobalSpace(space_id="sp-b", owning_instance="owner.home", status="banned")
    )
    assert (await home.post(f"{LIST}/refresh", headers=ADMIN)).status == 202
    assert await _eventually(home, {"sp-a"}) == {"sp-a"}


async def test_a_pending_connection_lists_nothing(aiohttp_client, tmp_dir):
    """A connection still awaiting the GFS operator lists none of its
    spaces: nothing can be joined through it yet."""
    gfs = await _gfs(aiohttp_client, tmp_dir)
    await gfs.server.app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
    await _seed_global_space(gfs)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    r = await home.post(PATH, headers=ADMIN)
    assert (await r.json())["status"] == "pending"
    assert await _eventually(home, set(), timeout=0.5) == set()
